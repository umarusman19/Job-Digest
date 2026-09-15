"""Relevance rules.

Everything here is a pure function over a Posting. No network, no state, no
model calls -- which means the same posting always gets the same verdict, and a
disagreement about a verdict is settled by reading twelve lines of code instead
of re-running a prompt.

The exclusion list is the part that actually earns its place. Six weeks of
applying taught me that most wasted applications are not near-misses; they are
categories I had already decided against and then applied to anyway at 2am.
Encoding that decision once is worth more than any ranking algorithm.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from .sources import Posting


@dataclass(frozen=True)
class Verdict:
    keep: bool
    reason: str


@dataclass
class Rules:
    """Loaded from config.yaml. See config.example.yaml for the shape."""

    title_include: Sequence[str]
    title_exclude: Sequence[str]
    location_allow: Sequence[str]
    location_deny: Sequence[str]
    require_location_match: bool = True

    @classmethod
    def from_config(cls, cfg: dict) -> "Rules":
        f = cfg.get("filters", {})
        return cls(
            title_include=[s.lower() for s in f.get("title_include", [])],
            title_exclude=[s.lower() for s in f.get("title_exclude", [])],
            location_allow=[s.lower() for s in f.get("location_allow", [])],
            location_deny=[s.lower() for s in f.get("location_deny", [])],
            require_location_match=bool(f.get("require_location_match", True)),
        )


def _contains_any(haystack: str, needles: Iterable[str]) -> str | None:
    """Return the first needle found, or None. Word-boundary aware.

    Substring matching is wrong here: 'ui' matched 'building' and 'guide',
    and 'pm' matched 'compliance'. That class of false positive is how a
    filter quietly stops filtering.
    """
    for needle in needles:
        if re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack):
            return needle
    return None


def judge(posting: Posting, rules: Rules) -> Verdict:
    title = posting.title.lower()
    location = posting.location.lower()

    # Exclusions first, and deliberately so: a role can match "product
    # designer" and still be a Product Manager req. Excluding after including
    # would let it through.
    hit = _contains_any(title, rules.title_exclude)
    if hit:
        return Verdict(False, f"title excluded on '{hit}'")

    if rules.title_include:
        hit = _contains_any(title, rules.title_include)
        if not hit:
            return Verdict(False, "title matched no include term")

    hit = _contains_any(location, rules.location_deny)
    if hit:
        return Verdict(False, f"location denied on '{hit}'")

    if rules.location_allow:
        hit = _contains_any(location, rules.location_allow)
        if not hit:
            if rules.require_location_match:
                return Verdict(False, "location matched no allow term")
            return Verdict(True, "kept: location unmatched but not required")

    return Verdict(True, "kept")


def apply_rules(postings: Sequence[Posting], rules: Rules
                ) -> tuple[list[Posting], dict[str, int]]:
    """Filter, and return a histogram of why things were dropped.

    The histogram matters. A filter that silently drops everything looks
    identical to a quiet week, and telling those apart after the fact is
    impossible without it.
    """
    kept: list[Posting] = []
    dropped: dict[str, int] = {}
    for p in postings:
        v = judge(p, rules)
        if v.keep:
            kept.append(p)
        else:
            dropped[v.reason] = dropped.get(v.reason, 0) + 1
    return kept, dropped
