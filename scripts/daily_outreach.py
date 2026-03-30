#!/usr/bin/env python3
"""
daily_outreach.py - Send personalized outreach emails to leads.

Usage:
    python3 daily_outreach.py
"""

import sys
import os
import csv
import json
import smtplib
import tempfile
import shutil
import re
import subprocess
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

try:
    import anthropic
except ImportError:
    sys.exit("Missing dependency: pip install anthropic")

# ── Config ────────────────────────────────────────────────────────────────────

LEADS_CSV = Path("/root/.picoclaw/workspace/state/outreach/leads.csv")
LOG_JSONL = Path("/root/.picoclaw/workspace/state/outreach/daily_outreach_log.jsonl")
CONFIG_JSON = Path("/root/.picoclaw/config.json")
EMAIL_ENV_SH = Path("/root/.picoclaw/workspace/email_env_private.sh")

SMTP_HOST = "mail.mhbllcmobilenotary.com"
SMTP_PORT = 465
SMTP_USER = "ambrown@mhbllcmobilenotary.com"
SENDER_NAME = "MHB Mobile Notary"

CLAUDE_MODEL = "claude-haiku-4-5-20251001"

CSV_HEADERS = ["created_at", "name", "firm", "email", "city", "state", "notes", "status"]


# ── Credentials ───────────────────────────────────────────────────────────────

def load_anthropic_key() -> str:
    if not CONFIG_JSON.exists():
        sys.exit(f"Config file not found: {CONFIG_JSON}")
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            cfg = json.load(f)
        key = cfg["providers"]["anthropic"]["api_key"]
        if not key:
            raise ValueError("api_key is empty")
        return key
    except (KeyError, ValueError) as exc:
        sys.exit(f"Cannot read Anthropic API key from {CONFIG_JSON}: {exc}")


def load_email_password() -> str:
    """Parse EMAIL_PASSWORD from the env file (bash export or plain assignment)."""
    if not EMAIL_ENV_SH.exists():
        sys.exit(f"Email env file not found: {EMAIL_ENV_SH}")
    content = EMAIL_ENV_SH.read_text(encoding="utf-8")
    # Match: export EMAIL_PASSWORD="..." or EMAIL_PASSWORD='...' or EMAIL_PASSWORD=...
    pat = re.compile(
        r"""(?:export\s+)?EMAIL_PASSWORD\s*=\s*(?P<q>['"]?)(?P<val>.*?)(?P=q)\s*$""",
        re.MULTILINE,
    )
    m = pat.search(content)
    if not m:
        sys.exit(f"EMAIL_PASSWORD not found in {EMAIL_ENV_SH}")
    password = m.group("val")
    if not password:
        sys.exit("EMAIL_PASSWORD is empty")
    return password


# ── Logging ───────────────────────────────────────────────────────────────────

def log_result(entry: dict) -> None:
    LOG_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ── CSV helpers ───────────────────────────────────────────────────────────────

def read_leads() -> list[dict]:
    if not LEADS_CSV.exists():
        return []
    with open(LEADS_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def write_leads(leads: list[dict]) -> None:
    """Write all leads back to CSV atomically."""
    LEADS_CSV.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEADS_CSV.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for lead in leads:
            writer.writerow({k: lead.get(k, "") for k in CSV_HEADERS})
    shutil.move(str(tmp), str(LEADS_CSV))


# ── Email generation ──────────────────────────────────────────────────────────

def generate_email(client: anthropic.Anthropic, lead: dict) -> tuple[str, str]:
    """Return (subject, body) using Claude."""
    firm = lead.get("firm") or "your firm"
    city = lead.get("city") or "your city"
    name = lead.get("name") or ""

    greeting = f"Hi {name}," if name else "Hello,"

    prompt = (
        f"Write a short, professional outreach email (under 150 words, plain text, no bullet points) "
        f"from MHB Mobile Notary introducing our remote online notary (RON) services. "
        f"The recipient works at '{firm}' in {city}. "
        f"Mention the firm name and city naturally. "
        f"Include one clear call to action to schedule a brief call. "
        f"Start the email body with: '{greeting}'\n"
        f"Do NOT include a subject line in the body. "
        f"Sign off as: Amanda Brown | MHB Mobile Notary | ambrown@mhbllcmobilenotary.com"
    )

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    body = message.content[0].text.strip()

    subject = f"Remote Online Notary Services for {firm}"
    return subject, body


# ── SMTP send ─────────────────────────────────────────────────────────────────

def send_email(password: str, to_email: str, subject: str, body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{SENDER_NAME} <{SMTP_USER}>"
    msg["To"] = to_email
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(SMTP_USER, password)
        server.sendmail(SMTP_USER, [to_email], msg.as_string())


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    anthropic_key = load_anthropic_key()
    email_password = load_email_password()

    client = anthropic.Anthropic(api_key=anthropic_key)

    leads = read_leads()
    if not leads:
        print("No leads found. Run find_leads.py first.")
        return

    sent_count = 0
    skipped_count = 0
    failed_count = 0
    modified = False

    for lead in leads:
        status = (lead.get("status") or "").strip().lower()
        email = (lead.get("email") or "").strip()
        firm = lead.get("firm") or "Unknown"
        name = lead.get("name") or ""

        if status == "sent":
            skipped_count += 1
            continue
        if not email:
            skipped_count += 1
            continue

        # Generate email via Claude
        subject = ""
        body = ""
        try:
            subject, body = generate_email(client, lead)
        except Exception as exc:
            err = f"Claude generation failed: {exc}"
            print(f"  [fail] {firm} ({email}): {err}")
            failed_count += 1
            log_result(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "recipient_name": name,
                    "firm": firm,
                    "email": email,
                    "subject": subject,
                    "status": "failed",
                    "error": err,
                }
            )
            continue

        # Send email via SMTP
        try:
            send_email(email_password, email, subject, body)
            lead["status"] = "sent"
            modified = True
            sent_count += 1
            print(f"  [sent] {firm} → {email}")
            log_result(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "recipient_name": name,
                    "firm": firm,
                    "email": email,
                    "subject": subject,
                    "status": "sent",
                    "error": None,
                }
            )
        except Exception as exc:
            err = f"SMTP send failed: {exc}"
            print(f"  [fail] {firm} ({email}): {err}")
            failed_count += 1
            log_result(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "recipient_name": name,
                    "firm": firm,
                    "email": email,
                    "subject": subject,
                    "status": "failed",
                    "error": err,
                }
            )

    if modified:
        write_leads(leads)

    print(f"\nDone. {sent_count} sent, {skipped_count} skipped, {failed_count} failed.")
    print(f"Log: {LOG_JSONL}")


if __name__ == "__main__":
    main()
