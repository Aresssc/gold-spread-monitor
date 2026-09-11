# -*- coding: utf-8 -*-
"""Email the GLD monitor report (from output/) via SMTP.

Reads secrets from env vars:
  SMTP_HOST (default smtp.qq.com)  SMTP_PORT (default 465)
  SMTP_USER (sender email, e.g. you@qq.com)
  SMTP_PASS (SMTP authorization code, NOT the login password)
  MAIL_TO   (recipient, defaults to SMTP_USER)

Skips silently if SMTP_USER/SMTP_PASS not set (so local runs don't fail).
"""
import json
import os
import smtplib
import sys
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

OUT_DIR = os.environ.get("OUT_DIR", "output")


def main():
    user = os.environ.get("SMTP_USER", "")
    pwd = os.environ.get("SMTP_PASS", "")
    if not user or not pwd:
        print("[mail] SMTP_USER/SMTP_PASS not set, skip sending.")
        return

    to = os.environ.get("MAIL_TO", user)
    host = os.environ.get("SMTP_HOST", "smtp.qq.com")
    port = int(os.environ.get("SMTP_PORT", "465"))

    with open(f"{OUT_DIR}/gld_monitor_data.json", encoding="utf-8") as f:
        d = json.load(f)
    with open(f"{OUT_DIR}/gld_monitor_report.md", encoding="utf-8") as f:
        report = f.read()
    png_path = f"{OUT_DIR}/gld_monitor_oi.png"

    spot = d.get("spot", 0)
    subj_date = d.get("generated_at", "")[:10]
    subject = f"GLD 期权日报 {subj_date} — spot {spot:.2f}"

    # minimal markdown -> text (keep tables as-is, most mail clients ok in monospace)
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = formataddr(("GLD Monitor", user))
    msg["To"] = to
    body = MIMEText(report, "plain", "utf-8")
    body.add_header("Content-Disposition", "inline")
    msg.attach(body)
    if os.path.exists(png_path):
        with open(png_path, "rb") as f:
            att = MIMEApplication(f.read(), _subtype="png")
        att.add_header("Content-Disposition", "attachment",
                       filename="gld_monitor_oi.png")
        msg.attach(att)

    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls()
    try:
        server.login(user, pwd)
        server.sendmail(user, [to], msg.as_string())
        print(f"[mail] sent to {to}: {subject}")
    finally:
        server.quit()


if __name__ == "__main__":
    sys.exit(main())
