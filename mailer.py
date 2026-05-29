"""Core email sending logic (used by both bot.py and send_emails.py)."""

import quopri
import re
import smtplib
import ssl
import time
import uuid
from email import policy
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.parser import Parser
from email.utils import formatdate, formataddr
from html.parser import HTMLParser
from string import Template
from typing import Generator


CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")


# ─── Parsing ────────────────────────────────────────────────────────────────

def parse_addresses(text: str) -> list[dict]:
    """Parse recipients from raw file content (semicolon-separated)."""
    email_pattern = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s;,\n]+")
    recipients: list[dict] = []
    seen: set[str] = set()

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(";") if p.strip()]
        for i, part in enumerate(parts):
            if email_pattern.fullmatch(part) and part not in seen:
                name = (
                    parts[i + 1]
                    if i + 1 < len(parts) and "@" not in parts[i + 1]
                    else ""
                )
                recipients.append({"email": part, "name": name})
                seen.add(part)

    return recipients


def contains_cyrillic(text: str) -> bool:
    return bool(CYRILLIC_RE.search(text))


# ─── Template ───────────────────────────────────────────────────────────────

class TemplateError(ValueError):
    pass


def normalize_template(raw_template: str) -> tuple[str, list[str]]:
    """Return clean HTML from a regular HTML file or an MHTML/raw MIME export."""
    warnings: list[str] = []
    template = raw_template.lstrip("\ufeff").strip()

    if _looks_like_mhtml(template):
        extracted = _extract_html_from_mhtml(template)
        if not extracted:
            raise TemplateError(
                "Template looks like MHTML/raw MIME, but no text/html part was found. "
                "Please upload a clean .html file, not 'Webpage, Complete' or .mht/.mhtml."
            )
        template = extracted.strip()
        warnings.append("Extracted the HTML part from an MHTML/raw MIME file.")

    template = _strip_snapshot_headers(template)
    template = _remove_unsafe_local_references(template)
    _validate_clean_html(template)
    if contains_cyrillic(template):
        raise TemplateError("Template contains Cyrillic text. Use English-only email content.")
    return template, warnings


def _looks_like_mhtml(text: str) -> bool:
    head = text[:8000].lower()
    return (
        "multipart/related" in head
        or "snapshot-content-location:" in head
        or "content-location: file://" in head
        or "content-transfer-encoding: quoted-printable" in head
    )


def _extract_html_from_mhtml(text: str) -> str:
    parsed = Parser(policy=policy.default).parsestr(text)
    if parsed.is_multipart():
        for part in parsed.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload is not None:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
                return part.get_content()

    boundary_match = re.search(
        r'boundary=(?:"([^"]+)"|([^;\s]+))',
        text,
        re.IGNORECASE,
    )
    if not boundary_match:
        return _extract_single_quoted_printable_html(text)

    boundary = boundary_match.group(1) or boundary_match.group(2)
    parts = re.split(rf"(?:\r?\n|^)--{re.escape(boundary)}(?:--)?\s*", text)
    for part in parts:
        if re.search(r"(?im)^Content-Type:\s*text/html\b", part):
            headers, body = _split_mime_part(part)
            if re.search(r"(?im)^Content-Transfer-Encoding:\s*quoted-printable\b", headers):
                body = quopri.decodestring(body.encode("utf-8", errors="replace")).decode(
                    "utf-8", errors="replace"
                )
            return body

    return ""


def _extract_single_quoted_printable_html(text: str) -> str:
    headers, body = _split_mime_part(text)
    if "text/html" not in headers.lower():
        return ""
    if "quoted-printable" in headers.lower():
        return quopri.decodestring(body.encode("utf-8", errors="replace")).decode(
            "utf-8", errors="replace"
        )
    return body


def _split_mime_part(text: str) -> tuple[str, str]:
    match = re.search(r"\r?\n\r?\n", text)
    if not match:
        return text, ""
    return text[:match.start()], text[match.end():]


def _strip_snapshot_headers(template: str) -> str:
    template = re.sub(r"(?im)^\s*(From:\s*)?Snapshot-Content-Location:.*$", "", template)
    template = re.sub(r"(?im)^\s*Content-Location:\s*file://.*$", "", template)
    return template.strip()


def _remove_unsafe_local_references(template: str) -> str:
    template = re.sub(
        r"<img\b[^>]*\s(?:src|data-src)=[\"'](?:cid:|file:)[^\"']*[\"'][^>]*>",
        "",
        template,
        flags=re.IGNORECASE,
    )
    template = re.sub(
        r"\s(?:src|href|data-src)=[\"'](?:cid:|file:)[^\"']*[\"']",
        "",
        template,
        flags=re.IGNORECASE,
    )
    return template


def _validate_clean_html(template: str) -> None:
    lowered = template[:20000].lower()
    forbidden = (
        "snapshot-content-location:",
        "content-transfer-encoding:",
        "multipartboundary",
        "multipart/related",
        "file://",
        "------multipartboundary",
    )
    if any(marker in lowered for marker in forbidden):
        raise TemplateError(
            "Template still contains raw MIME headers, local file links, or embedded parts. "
            "Export/send a clean HTML file and use public https:// image URLs."
        )

    if "<html" not in lowered and "<body" not in lowered and "<table" not in lowered:
        raise TemplateError("Template does not look like HTML. Please upload an .html template.")

def render(template_str: str, recipient: dict) -> str:
    """Substitute $email, $name, $greeting placeholders in template."""
    name = recipient.get("name", "")
    if contains_cyrillic(name):
        name = ""
    greeting = f"Hello, {name}" if name else "Hello"
    return Template(template_str).safe_substitute(
        email=recipient["email"],
        name=name,
        greeting=greeting,
    )


class _StripHTML(HTMLParser):
    """Minimal HTML → plain text converter (no dependencies)."""

    _BLOCK = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"}
    _SKIP  = {"style", "script", "head"}

    def __init__(self):
        super().__init__()
        self._buf: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag in self._BLOCK:
            self._buf.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag in self._BLOCK:
            self._buf.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._buf.append(data)

    def result(self) -> str:
        text = "".join(self._buf)
        text = re.sub(r" {2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_text(html_str: str) -> str:
    p = _StripHTML()
    p.feed(html_str)
    return p.result()


def _extract_unsubscribe(html_str: str) -> str:
    """Return the first href that looks like an unsubscribe link."""
    m = re.search(
        r'href=["\']([^"\']*unsubscribe[^"\']*)["\']',
        html_str, re.IGNORECASE
    )
    return m.group(1) if m else ""


# ─── Sending ────────────────────────────────────────────────────────────────

def _build_message(
    sender_email: str,
    sender_name: str,
    recipient: dict,
    subject: str,
    html_body: str,
) -> MIMEMultipart:
    """Build a RFC-compliant multipart/alternative message."""
    msg = MIMEMultipart("alternative")

    # Required headers
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = f"<{uuid.uuid4()}@{sender_email.split('@')[-1]}>"

    # Encoded From / To (RFC 2047 handles non-ASCII names)
    msg["From"] = formataddr((sender_name, sender_email)) if sender_name else sender_email
    to_name = recipient.get("name", "")
    if contains_cyrillic(to_name):
        to_name = ""
    msg["To"] = formataddr((to_name, recipient["email"])) if to_name else recipient["email"]

    if contains_cyrillic(subject):
        raise TemplateError("Subject contains Cyrillic text. Use an English-only subject.")
    msg["Subject"] = subject
    msg["MIME-Version"] = "1.0"

    # List-Unsubscribe (helps avoid spam / unsubscribe button in Gmail)
    unsub = _extract_unsubscribe(html_body)
    if unsub:
        msg["List-Unsubscribe"] = f"<{unsub}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    # Plain text first, then HTML (mail clients prefer the last matching part)
    plain = html_to_text(html_body)
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    return msg


def send_all(
    recipients: list[dict],
    template_str: str,
    subject: str,
    sender_email: str,
    app_password: str,
    sender_name: str = "",
    delay: float = 1.0,
) -> Generator[dict, None, None]:
    """
    Send emails one by one. Yields progress dicts:
      {"index": i, "total": n, "email": "...", "ok": True/False, "error": "..."}
    """
    context = ssl.create_default_context()
    total = len(recipients)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(sender_email, app_password)
        for i, recipient in enumerate(recipients, 1):
            html = render(template_str, recipient)
            msg = _build_message(sender_email, sender_name, recipient, subject, html)
            try:
                server.sendmail(sender_email, recipient["email"], msg.as_string())
                yield {"index": i, "total": total, "email": recipient["email"], "ok": True}
            except Exception as e:
                yield {"index": i, "total": total, "email": recipient["email"], "ok": False, "error": str(e)}

            if i < total and delay > 0:
                time.sleep(delay)
