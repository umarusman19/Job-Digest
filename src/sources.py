"""Job sources: public ATS board endpoints.

Design note, and the reason this file exists at all:

The first version of this system expressed its fetch logic as a natural-language
instruction to an LLM ("read the boards, retry if one is blocked"). An LLM asked
to judge whether an HTTP fetch failed will answer differently on different runs.
Over two days it sent six "boards blocked / run failed" alerts, each followed
about ten minutes later by its own correction saying nothing had been wrong.

So the rule here is: a source either returns a list of postings or it returns an
error object. It never raises, never decides anything, and never notifies. The
caller reports partial success inline. See docs/postmortem.md.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

import requests

log = logging.getLogger(__name__)

USER_AGENT = "job-digest/1.0 (+https://github.com/USER/job-digest)"
TIMEOUT = 20
RETRIES = 3
BACKOFF_BASE = 1.5


@dataclass(frozen=True)
class Posting:
    """One job posting, normalised across ATS vendors."""

    uid: str          # stable across runs: "{ats}:{company}:{native_id}"
    company: str
    title: str
    location: str
    url: str
    ats: str
    posted_at: datetime | None = None

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.company} — {self.title} ({self.location})"


@dataclass
class SourceResult:
    """Outcome of reading one board. Success and failure are both data."""

    company: str
    ats: str
    postings: list[Posting] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _get_json(url: str, session: requests.Session) -> Any:
    """GET with bounded retries. Raises only after every attempt is spent.

    Retries on transport errors and on 5xx/429. A 404 is a permanent answer
    (wrong board slug) and is not retried -- the Apify version of this system
    silently returned zero rows for months because a wrong slug looked
    exactly like a company with no open roles.
    """
    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = session.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
            if resp.status_code == 404:
                raise PermanentSourceError(
                    f"404 for {url} -- board slug is probably wrong, "
                    "which is NOT the same as 'no open roles'"
                )
            if resp.status_code == 429 or resp.status_code >= 500:
                raise TransientSourceError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except PermanentSourceError:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
            last = exc
            if attempt < RETRIES:
                sleep_for = BACKOFF_BASE ** attempt
                log.warning("%s failed (attempt %d/%d): %s; retrying in %.1fs",
                            url, attempt, RETRIES, exc, sleep_for)
                time.sleep(sleep_for)
    raise TransientSourceError(f"{RETRIES} attempts failed for {url}: {last}")


class TransientSourceError(RuntimeError):
    """Worth retrying: timeout, reset, 429, 5xx."""


class PermanentSourceError(RuntimeError):
    """Not worth retrying: almost always a wrong board slug."""


# --------------------------------------------------------------------------- #
# Vendor adapters. Each takes raw JSON and returns Postings. Pure functions,
# so the tests exercise them against fixtures with no network involved.
# --------------------------------------------------------------------------- #

def _iso(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):  # Lever uses epoch milliseconds
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_greenhouse(company: str, payload: Any) -> list[Posting]:
    out = []
    for job in (payload or {}).get("jobs", []):
        loc = (job.get("location") or {}).get("name") or ""
        out.append(Posting(
            uid=f"greenhouse:{company}:{job['id']}",
            company=company,
            title=job.get("title", "").strip(),
            location=loc.strip(),
            url=job.get("absolute_url", ""),
            ats="greenhouse",
            posted_at=_iso(job.get("updated_at")),
        ))
    return out


def parse_lever(company: str, payload: Any) -> list[Posting]:
    out = []
    for job in payload or []:
        cats = job.get("categories") or {}
        out.append(Posting(
            uid=f"lever:{company}:{job['id']}",
            company=company,
            title=(job.get("text") or "").strip(),
            location=(cats.get("location") or "").strip(),
            url=job.get("hostedUrl", ""),
            ats="lever",
            posted_at=_iso(job.get("createdAt")),
        ))
    return out


def parse_ashby(company: str, payload: Any) -> list[Posting]:
    out = []
    for job in (payload or {}).get("jobs", []):
        if job.get("isListed") is False:
            continue
        out.append(Posting(
            uid=f"ashby:{company}:{job['id']}",
            company=company,
            title=(job.get("title") or "").strip(),
            location=(job.get("location") or "").strip(),
            url=job.get("jobUrl", ""),
            ats="ashby",
            posted_at=_iso(job.get("publishedAt")),
        ))
    return out


ADAPTERS: dict[str, tuple[str, Callable[[str, Any], list[Posting]]]] = {
    "greenhouse": (
        "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        parse_greenhouse,
    ),
    "lever": (
        "https://api.lever.co/v0/postings/{slug}?mode=json",
        parse_lever,
    ),
    "ashby": (
        "https://api.ashbyhq.com/posting-api/job-board/{slug}",
        parse_ashby,
    ),
}


def fetch_board(company: str, ats: str, slug: str,
                session: requests.Session | None = None) -> SourceResult:
    """Read one board. Always returns a SourceResult; never raises."""
    ats = ats.lower().strip()
    if ats not in ADAPTERS:
        return SourceResult(company, ats, error=f"unknown ats '{ats}'")

    template, parse = ADAPTERS[ats]
    session = session or requests.Session()
    try:
        payload = _get_json(template.format(slug=slug), session)
    except Exception as exc:  # noqa: BLE001
        return SourceResult(company, ats, error=str(exc))

    try:
        return SourceResult(company, ats, postings=parse(company, payload))
    except Exception as exc:  # noqa: BLE001
        return SourceResult(company, ats, error=f"parse failed: {exc}")


def fetch_all(boards: Sequence[dict]) -> tuple[list[Posting], list[SourceResult]]:
    """Read every configured board.

    Returns (all postings, results). The caller decides what to do about
    failures; this function does not notify anybody about anything.
    """
    session = requests.Session()
    results = [
        fetch_board(b["company"], b["ats"], b["slug"], session=session)
        for b in boards
    ]
    postings = [p for r in results if r.ok for p in r.postings]
    return postings, results
