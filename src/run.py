"""Entry point. Orchestration only -- no logic worth hiding in here.

    python -m src.run --config config.yaml
    python -m src.run --config config.yaml --dry-run   # print, send nothing

Exit codes
    0  ran, digest sent (or dry-run printed)
    1  configuration or credential problem -- the run could not start
    2  every single source failed, which is a real signal rather than noise

A run where *some* sources failed still exits 0 and still sends. That is the
deliberate choice this whole repository is organised around: see render.py.
"""

from __future__ import annotations

import argparse
import logging
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

import yaml

from . import inbox as inbox_mod
from .filters import Rules, apply_rules
from .render import render_text, subject_line
from .sources import fetch_all
from .store import Store

log = logging.getLogger("job-digest")
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def send_email(to: str, subject: str, body: str,
               user: str, app_password: str) -> None:
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.login(user, app_password)
        s.send_message(msg)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Job digest")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--state", default="state/seen.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the digest, send nothing, write no state")
    ap.add_argument("--skip-inbox", action="store_true",
                    help="skip application tracking (no mailbox credentials needed)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        log.error("no config at %s -- copy config.example.yaml and edit it",
                  cfg_path)
        return 1
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    boards = cfg.get("boards") or []
    if not boards:
        log.error("config has no boards")
        return 1

    postings, results = fetch_all(boards)
    if all(not r.ok for r in results):
        # Every source failing is categorically different from some failing.
        # It usually means no network at all, and it is the one case worth
        # exiting non-zero so the Actions run goes red.
        log.error("all %d sources failed", len(results))
        for r in results:
            log.error("  %s (%s): %s", r.company, r.ats, r.error)
        return 2

    kept, dropped = apply_rules(postings, Rules.from_config(cfg))

    store = Store.load(args.state)
    new = store.new_among(kept)

    stats = None
    if not args.skip_inbox and cfg.get("track_applications", True):
        user = os.environ.get("GMAIL_USER")
        pw = os.environ.get("GMAIL_APP_PASSWORD")
        if user and pw:
            try:
                window = int(cfg.get("application_window_days", 90))
                apps, unreadable = inbox_mod.read_mailbox(user, pw, window)
                stats = inbox_mod.summarise(apps, window, unreadable=unreadable)
            except Exception as exc:  # noqa: BLE001
                # Mailbox trouble must not cost you the job listings.
                log.warning("application tracking skipped: %s", exc)
        else:
            log.info("GMAIL_USER / GMAIL_APP_PASSWORD not set; "
                     "skipping application tracking")

    body = render_text(new, results, dropped, stats)
    subject = subject_line(new, results)

    if args.dry_run:
        print(subject)
        print()
        print(body)
        return 0

    to = cfg.get("email_to") or os.environ.get("GMAIL_USER")
    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if not (to and user and pw):
        log.error("cannot send: set GMAIL_USER and GMAIL_APP_PASSWORD")
        return 1

    send_email(to, subject, body, user, pw)
    log.info("sent: %s", subject)

    store.mark(kept)
    removed = store.prune()
    store.save()
    log.info("state: %d seen, %d pruned", len(store.seen), removed)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
