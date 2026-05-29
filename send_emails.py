#!/usr/bin/env python3
"""CLI wrapper — sends bulk HTML emails via Gmail SMTP."""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import mailer

load_dotenv()


def main():
    parser = argparse.ArgumentParser(description="Send bulk HTML emails via Gmail SMTP")
    parser.add_argument("--addresses", "-a", required=True)
    parser.add_argument("--template", "-t", required=True)
    parser.add_argument("--subject", "-s", default=os.getenv("EMAIL_SUBJECT", "No Subject"))
    parser.add_argument("--delay", type=float, default=float(os.getenv("EMAIL_DELAY", "1.0")))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sender_email = os.getenv("GMAIL_USER")
    app_password = os.getenv("GMAIL_APP_PASSWORD")
    sender_name = os.getenv("SENDER_NAME", "")

    if not sender_email or not app_password:
        print("[ERROR] GMAIL_USER and GMAIL_APP_PASSWORD must be set in .env")
        sys.exit(1)

    addr_path = Path(args.addresses)
    tmpl_path = Path(args.template)
    for p in (addr_path, tmpl_path):
        if not p.exists():
            print(f"[ERROR] File not found: {p}")
            sys.exit(1)

    recipients = mailer.parse_addresses(addr_path.read_text(encoding="utf-8"))
    try:
        template_str, warnings = mailer.normalize_template(tmpl_path.read_text(encoding="utf-8"))
    except mailer.TemplateError as e:
        print(f"[ERROR] Bad template: {e}")
        sys.exit(1)

    print(f"Sender    : {sender_email}")
    print(f"Subject   : {args.subject}")
    print(f"Recipients: {len(recipients)}")
    for warning in warnings:
        print(f"[WARN] {warning}")
    print("-" * 40)

    if args.dry_run:
        print("[DRY RUN] Would send to:")
        for r in recipients:
            print(f"  {r['email']}" + (f" ({r['name']})" if r["name"] else ""))
        return

    sent = failed = 0
    for r in mailer.send_all(recipients, template_str, args.subject,
                             sender_email, app_password, sender_name, args.delay):
        mark = "OK" if r["ok"] else f"FAIL — {r.get('error', '')}"
        print(f"[{r['index']}/{r['total']}] {r['email']} ... {mark}")
        if r["ok"]:
            sent += 1
        else:
            failed += 1

    print("-" * 40)
    print(f"Done. Sent: {sent}  Failed: {failed}")


if __name__ == "__main__":
    main()
