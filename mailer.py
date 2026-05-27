"""Core email sending logic (used by both bot.py and send_emails.py)."""

import re
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from string import Template
from typing import Generator


def parse_addresses(text: str) -> list[dict]:
    """Parse addresses from raw file content (semicolon-separated)."""
    email_pattern = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s;,\n]+")
    recipients = []
    seen = set()

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


def render(template_str: str, recipient: dict) -> str:
    """Substitute $email and $name in template."""
    return Template(template_str).safe_substitute(
        email=recipient["email"],
        name=recipient.get("name", ""),
    )


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
    from_addr = f"{sender_name} <{sender_email}>" if sender_name else sender_email

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(sender_email, app_password)
        for i, recipient in enumerate(recipients, 1):
            html = render(template_str, recipient)
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = from_addr
            msg["To"] = recipient["email"]
            msg.attach(MIMEText(html, "html", "utf-8"))
            try:
                server.sendmail(sender_email, recipient["email"], msg.as_string())
                yield {"index": i, "total": total, "email": recipient["email"], "ok": True}
            except Exception as e:
                yield {"index": i, "total": total, "email": recipient["email"], "ok": False, "error": str(e)}

            if i < total and delay > 0:
                import time
                time.sleep(delay)
