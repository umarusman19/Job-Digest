"""Digest composition.

The single most important rule in this repository lives here: **one run sends
at most one email, and it always contains everything.**

The version this replaced sent a separate message whenever a source refused a
fetch. Over two days that produced six alarm emails and six corrections, and
the practical result was that I stopped reading the digest -- which is the only
failure mode a notification system cannot survive. Source failures are now a
line inside the normal digest, next to the jobs. A partial read is reported as
"9 of 12 sources read", which is information, not an emergency.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from .inbox import Stats
from .sources import Posting, SourceResult


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def subject_line(new: Sequence[Posting], results: Sequence[SourceResult]) -> str:
    failed = [r for r in results if not r.ok]
    head = f"{_plural(len(new), 'new role')}" if new else "No new roles"
    if failed:
        return f"Job digest: {head} ({len(failed)} source(s) unread)"
    return f"Job digest: {head}"


def render_text(new: Sequence[Posting], results: Sequence[SourceResult],
                dropped: dict[str, int], stats: Stats | None,
                now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    lines: list[str] = []

    lines.append(f"Job digest — {now.strftime('%a %d %b %Y, %H:%M UTC')}")
    lines.append("=" * 58)
    lines.append("")
    lines.append(f"{len(ok)} of {len(results)} sources read. "
                 f"{_plural(len(new), 'new role')} after filtering.")
    lines.append("")

    if new:
        lines.append("NEW ROLES")
        lines.append("-" * 58)
        for p in sorted(new, key=lambda x: (x.company.lower(), x.title.lower())):
            lines.append(f"  {p.company} — {p.title}")
            lines.append(f"    {p.location or 'location not stated'}")
            lines.append(f"    {p.url}")
            lines.append("")
    else:
        lines.append("No new roles matched. This is the normal result on most")
        lines.append("runs and does not mean anything is broken.")
        lines.append("")

    # Failures live here, inline, and nowhere else. No separate alert email.
    if failed:
        lines.append("SOURCES NOT READ")
        lines.append("-" * 58)
        for r in failed:
            lines.append(f"  {r.company} ({r.ats}): {r.error}")
        lines.append("")
        lines.append("  A source listed here was not read on this run. If the")
        lines.append("  same source appears for several consecutive runs, the")
        lines.append("  board slug is probably wrong — check it against")
        lines.append("  config.yaml. A one-off entry needs no action.")
        lines.append("")

    if stats is not None and stats.total:
        lines.append("YOUR APPLICATIONS")
        lines.append("-" * 58)
        lines.append(f"  Tracked in the last {stats.window_days} days: {stats.total}")
        lines.append(f"  Awaiting reply:  {stats.awaiting}")
        lines.append(f"  Rejected:        {stats.rejected}")
        lines.append(f"  Advanced:        {stats.advanced}")
        lines.append(f"  Ghosted (>{stats.ghost_threshold_days}d):   {stats.ghosted}")
        if stats.response_rate is not None:
            lines.append(f"  Response rate:   {stats.response_rate}%")
        if stats.median_hours_to_rejection is not None:
            lines.append("")
            lines.append(f"  Median time to rejection: "
                         f"{stats.median_hours_to_rejection:.0f}h")
            lines.append(f"  Fastest: {stats.fastest_rejection_hours:.0f}h  "
                         f"Slowest: {stats.slowest_rejection_hours:.0f}h")
            if stats.untimed_rejections:
                lines.append(f"  ({stats.untimed_rejections} rejection(s) had no "
                             "measurable gap — the only message from")
                lines.append("   that company was the rejection itself, so they "
                             "are counted but not timed.)")
            if stats.median_hours_to_rejection < 48:
                lines.append("")
                lines.append("  A median under 48h means these are automated")
                lines.append("  screens. Nobody opened a portfolio to decide.")
                lines.append("  Spend the effort on eligibility and targeting,")
                lines.append("  not on the work samples.")
        lines.append("")
        lines.append("  Classification is phrase-matching and will misread")
        lines.append("  unusual wording. Treat it as a trend, not a ledger.")
        lines.append("")

    if dropped:
        lines.append("FILTERED OUT")
        lines.append("-" * 58)
        for reason, n in sorted(dropped.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {n:>4}  {reason}")
        lines.append("")
        lines.append("  Shown so that a filter which has started dropping")
        lines.append("  everything looks different from a quiet week.")
        lines.append("")

    lines.append("-" * 58)
    lines.append("github.com/umarusman19/Job-Digest — runs on GitHub Actions")
    return "\n".join(lines)
