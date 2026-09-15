"""Seen-posting state.

"Only show me new roles" is the whole product. The first version delegated that
judgement to a model, which meant the same role could be new on Tuesday and old
on Wednesday. Here it is a set difference against a JSON file, which is boring
and cannot be wrong.

The state file is committed back to the repository by the workflow so that the
history of what the system saw is itself auditable -- but it stores only opaque
uids and dates, never company names or titles. See `docs/postmortem.md`.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

from .sources import Posting

SCHEMA = 1
DEFAULT_TTL_DAYS = 120


@dataclass
class Store:
    path: Path
    seen: dict[str, str] = field(default_factory=dict)  # uid -> ISO first-seen
    schema: int = SCHEMA

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Store":
        p = Path(path)
        if not p.exists():
            return cls(path=p)
        raw = json.loads(p.read_text(encoding="utf-8") or "{}")
        if raw.get("schema") != SCHEMA:
            # Forward compatibility: an unknown schema is treated as empty
            # rather than crashing the run. A missed digest is recoverable;
            # a crashed cron that nobody notices is not.
            return cls(path=p)
        return cls(path=p, seen=dict(raw.get("seen", {})), schema=SCHEMA)

    def new_among(self, postings: Sequence[Posting]) -> list[Posting]:
        return [p for p in postings if p.uid not in self.seen]

    def mark(self, postings: Iterable[Posting],
             now: datetime | None = None) -> None:
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        for p in postings:
            self.seen.setdefault(p.uid, stamp)

    def prune(self, ttl_days: int = DEFAULT_TTL_DAYS,
              now: datetime | None = None) -> int:
        """Drop entries older than the TTL so the file cannot grow forever.

        A role that gets reposted after the TTL will surface again. That is
        the intended trade: a duplicate every four months is cheaper than an
        unbounded state file.
        """
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=ttl_days)
        before = len(self.seen)
        self.seen = {
            uid: ts for uid, ts in self.seen.items()
            if _parse(ts) is None or _parse(ts) >= cutoff  # keep unparseable
        }
        return before - len(self.seen)

    def save(self) -> None:
        """Atomic write. A cron job killed mid-write must not corrupt state."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"schema": SCHEMA, "seen": self.seen}, indent=1, sort_keys=True
        )
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
