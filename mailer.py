"""Core email sending logic (used by both bot.py and send_emails.py)."""

import re
import smtplib
import ssl
import time
import uuid
from email.headerregistry import Address
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, formataddr
from html.parser import HTMLParser
from string import Template
from typing import Generator


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


# ─── Template ───────────────────────────────────────────────────────────────

def render(template_str: str, recipient: dict) -> str:
    """Substitute $email, $name, $greeting placeholders in template."""
    name = recipient.get("name", "")
    greeting = f"Привет, {name}" if name else "Привет"
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
    msg["To"] = formataddr((to_name, recipient["email"])) if to_name else recipient["email"]

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
