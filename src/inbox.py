"""Application tracking from your own mailbox, over IMAP.

Why this module exists
----------------------
Counting job postings is easy and nearly useless. The interesting question is
what happens *after* you apply, and the answer is sitting in your mailbox
already. Instrumenting that turned out to be the finding that changed how I
apply: across my first batch of applications the rejections arrived in a median
of well under two days, which is far too fast for a human to have opened
anything. That is a measurement, not a feeling, and it says the bottleneck is
eligibility and targeting rather than portfolio quality.

Privacy design, which is the part I would want a reviewer to read
-----------------------------------------------------------------
This repository is public. My mailbox is not. So the boundary is enforced in
code rather than by remembering:

* `Application` holds a company name and a subject line. It exists only in
  memory, only long enough to render an email to myself.
* `Stats` is the only thing allowed to leave the process -- written to disk,
  committed, or put in a README. It holds counts and durations. It has no
  field capable of carrying a company name, and `to_public_dict()` whitelists
  keys explicitly rather than serialising the object.
* `tests/test_inbox.py::test_public_stats_carry_no_identifiers` asserts this,
  so it is a build failure rather than a good intention.

Credentials are never in this repo and never in config. The workflow reads
`GMAIL_USER` and `GMAIL_APP_PASSWORD` from GitHub Actions secrets, which you
set yourself. Use a Google App Password, not your account password.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import logging
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

log = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
GHOST_AFTER_DAYS = 21

# Heuristics, and openly so. These are phrase patterns observed across a few
# dozen real ATS emails; they are not a classifier and they will misread
# unusual wording. The digest prints its own confidence so a bad call is
# visible rather than silent.
REJECTED = re.compile(
    r"\b(?:not (?:be )?(?:moving|progress\w*) forward"
    r"|decided (?:not )?to (?:move|proceed|progress)"
    r"|other candidates?"
    r"|will not be (?:progress\w*|proceeding)"
    r"|unsuccessful (?:on )?this occasion"
    r"|not (?:to )?be (?:taking|proceeding)"
    r"|no longer under consideration"
    r"|position has been filled"
    r"|pursue other applicants?)\b",
    re.I,
)
ADVANCED = re.compile(
    r"\b(?:schedule (?:a|an|your)"
    r"|book (?:a|your) (?:call|slot|time)"
    r"|next steps?"
    r"|would (?:like|love) to (?:speak|chat|meet)"
    r"|invite you to"
    r"|technical (?:interview|screen)"
    r"|take[- ]home"
    r"|offer letter)\b",
    re.I,
)
ACKNOWLEDGED = re.compile(
    r"\b(?:thank you for (?:your )?(?:applying|application|interest)"
    r"|we(?:'ve| have) received your application"
    r"|application (?:received|submitted|confirmation)"
    r"|thanks for applying)\b",
    re.I,
)

Status = str  # one of: awaiting | rejected | advanced | ghosted


@dataclass
class Application:
    """PRIVATE. Never persisted, never serialised, never committed."""

    message_id: str
    company: str
    subject: str
    applied_at: datetime
    status: Status = "awaiting"
    responded_at: datetime | None = None

    @property
    def hours_to_response(self) -> float | None:
        if self.responded_at is None:
            return None
        return (self.responded_at - self.applied_at).total_seconds() / 3600


@dataclass
class Stats:
    """PUBLIC. Counts and durations only. Structurally incapable of leaking."""

    total: int = 0
    awaiting: int = 0
    rejected: int = 0
    advanced: int = 0
    ghosted: int = 0
    median_hours_to_rejection: float | None = None
    fastest_rejection_hours: float | None = None
    slowest_rejection_hours: float | None = None
    ghost_threshold_days: int = GHOST_AFTER_DAYS
    window_days: int = 0
    unreadable_messages: int = 0

    _PUBLIC_KEYS = (
        "total", "awaiting", "rejected", "advanced", "ghosted",
        "median_hours_to_rejection", "fastest_rejection_hours",
        "slowest_rejection_hours", "ghost_threshold_days", "window_days",
        "unreadable_messages",
    )

    def to_public_dict(self) -> dict:
        """Explicit whitelist. Do not replace with dataclasses.asdict().

        asdict() would silently start exporting any field a future edit adds,
        which is exactly how a redaction boundary stops holding.
        """
        return {k: getattr(self, k) for k in self._PUBLIC_KEYS}

    @property
    def response_rate(self) -> float | None:
        answered = self.rejected + self.advanced
        return None if not self.total else round(100 * answered / self.total, 1)


def _sender_domain(raw: str) -> str:
    addr = email.utils.parseaddr(raw or "")[1]
    return addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""


def _company_from_domain(domain: str) -> str:
    """Best-effort display name. Private-use only."""
    parts = [p for p in domain.split(".") if p not in {"com", "co", "io",
             "net", "org", "uk", "ai", "dev", "app", "jobs", "mail", "email"}]
    ats = {"greenhouse", "lever", "ashbyhq", "workable", "myworkday",
           "smartrecruiters", "teamtailor", "recruitee", "bamboohr"}
    named = [p for p in parts if p not in ats]
    return (named[0] if named else (parts[0] if parts else domain)).title()


def classify(subject: str, body: str) -> Status:
    """Order matters: a rejection often also mentions 'next steps'."""
    blob = f"{subject}\n{body[:4000]}"
    if REJECTED.search(blob):
        return "rejected"
    if ADVANCED.search(blob):
        return "advanced"
    return "awaiting"


def summarise(apps: Sequence[Application], window_days: int,
              now: datetime | None = None,
              unreadable: int = 0) -> Stats:
    """Collapse private Applications into publishable Stats."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=GHOST_AFTER_DAYS)

    s = Stats(total=len(apps), window_days=window_days,
              unreadable_messages=unreadable)
    rejection_hours: list[float] = []

    for a in apps:
        status = a.status
        if status == "awaiting" and a.applied_at < cutoff:
            status = "ghosted"
        if status == "rejected":
            s.rejected += 1
            h = a.hours_to_response
            if h is not None and h >= 0:
                rejection_hours.append(h)
        elif status == "advanced":
            s.advanced += 1
        elif status == "ghosted":
            s.ghosted += 1
        else:
            s.awaiting += 1

    if rejection_hours:
        s.median_hours_to_rejection = round(statistics.median(rejection_hours), 1)
        s.fastest_rejection_hours = round(min(rejection_hours), 1)
        s.slowest_rejection_hours = round(max(rejection_hours), 1)
    return s


def read_mailbox(user: str, app_password: str, window_days: int = 90,
                 host: str = IMAP_HOST) -> tuple[list[Application], int]:
    """Pull application-related threads. Returns (applications, unreadable).

    Read-only: the connection is opened with readonly=True so a bug here
    cannot mark, move or delete anything in the mailbox.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=window_days))
    apps: dict[str, Application] = {}
    unreadable = 0

    conn = imaplib.IMAP4_SSL(host)
    try:
        conn.login(user, app_password)
        conn.select("INBOX", readonly=True)
        typ, data = conn.search(None, f'(SINCE "{since.strftime("%d-%b-%Y")}")')
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ}")

        ids = (data[0] or b"").split()
        log.info("scanning %d messages since %s", len(ids), since.date())

        for mid in ids:
            typ, raw = conn.fetch(mid, "(RFC822)")
            if typ != "OK" or not raw or not isinstance(raw[0], tuple):
                unreadable += 1
                continue
            try:
                msg = email.message_from_bytes(raw[0][1])
                subject = str(email.header.make_header(
                    email.header.decode_header(msg.get("Subject", ""))))
                body = _plain_body(msg)
                blob = f"{subject}\n{body[:4000]}"
                if not (ACKNOWLEDGED.search(blob) or REJECTED.search(blob)
                        or ADVANCED.search(blob)):
                    continue

                domain = _sender_domain(msg.get("From", ""))
                company = _company_from_domain(domain)
                when = email.utils.parsedate_to_datetime(msg.get("Date"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)

                status = classify(subject, body)
                key = company or domain or str(mid)
                existing = apps.get(key)
                if existing is None:
                    apps[key] = Application(
                        message_id=msg.get("Message-ID", str(mid)),
                        company=company, subject=subject, applied_at=when,
                        status="awaiting" if status == "awaiting" else status,
                        responded_at=None if status == "awaiting" else when,
                    )
                else:
                    # Earliest message is the application; later ones are the
                    # outcome. Keeps one row per company rather than per email.
                    if when < existing.applied_at:
                        existing.applied_at = when
                    if status != "awaiting" and existing.status == "awaiting":
                        existing.status = status
                        existing.responded_at = when
            except Exception as exc:  # noqa: BLE001
                log.warning("unreadable message: %s", exc)
                unreadable += 1
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001,S110
            pass

    return list(apps.values()), unreadable


def _plain_body(msg: email.message.Message) -> str:
    if not msg.is_multipart():
        payload = msg.get_payload(decode=True) or b""
        return payload.decode(msg.get_content_charset() or "utf-8", "replace")
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True) or b""
            return payload.decode(part.get_content_charset() or "utf-8", "replace")
    return ""
