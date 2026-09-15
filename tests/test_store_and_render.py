"""State and digest tests.

`test_digest_never_splits_failures_into_a_separate_message` is the regression
test for the defect this rewrite exists to fix.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from src.inbox import Stats
from src.render import render_text, subject_line
from src.sources import Posting, SourceResult
from src.store import SCHEMA, Store

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def post(n: int) -> Posting:
    return Posting(uid=f"greenhouse:Acme:{n}", company="Acme",
                   title=f"Product Designer {n}", location="Remote",
                   url=f"https://example.com/{n}", ats="greenhouse")


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #

def test_new_among_is_a_set_difference(tmp_path):
    s = Store(path=tmp_path / "seen.json")
    batch = [post(1), post(2)]
    assert len(s.new_among(batch)) == 2
    s.mark(batch, now=NOW)
    assert s.new_among(batch) == []
    assert len(s.new_among(batch + [post(3)])) == 1


def test_roundtrip_survives_reload(tmp_path):
    p = tmp_path / "seen.json"
    s = Store(path=p)
    s.mark([post(1)], now=NOW)
    s.save()
    again = Store.load(p)
    assert again.schema == SCHEMA
    assert again.new_among([post(1)]) == []


def test_missing_file_loads_empty(tmp_path):
    assert Store.load(tmp_path / "nope.json").seen == {}


def test_unknown_schema_loads_empty_rather_than_crashing(tmp_path):
    """A crashed cron nobody notices is worse than one missed digest."""
    p = tmp_path / "seen.json"
    p.write_text(json.dumps({"schema": 9999, "seen": {"x": "y"}}))
    assert Store.load(p).seen == {}


def test_prune_drops_old_and_keeps_recent(tmp_path):
    s = Store(path=tmp_path / "seen.json")
    s.seen = {
        "old": (NOW - timedelta(days=200)).isoformat(),
        "new": (NOW - timedelta(days=5)).isoformat(),
        "junk": "not-a-date",
    }
    removed = s.prune(ttl_days=120, now=NOW)
    assert removed == 1
    assert "old" not in s.seen
    assert "new" in s.seen
    assert "junk" in s.seen  # unparseable is kept, not silently dropped


def test_state_file_holds_no_titles_or_companies(tmp_path):
    """The state file is committed by the workflow. It must be opaque."""
    p = tmp_path / "seen.json"
    s = Store(path=p)
    s.mark([post(1)], now=NOW)
    s.save()
    raw = p.read_text()
    assert "Product Designer" not in raw
    assert "example.com" not in raw


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    s = Store(path=tmp_path / "seen.json")
    s.mark([post(1)], now=NOW)
    s.save()
    assert [f.name for f in tmp_path.iterdir()] == ["seen.json"]


# --------------------------------------------------------------------------- #
# Render -- the one-email rule
# --------------------------------------------------------------------------- #

def test_digest_never_splits_failures_into_a_separate_message():
    """Regression test for the defect this rewrite exists to fix.

    The predecessor emailed an alarm the moment a fetch was refused, then
    emailed a correction ten minutes later. Six alarms and six corrections in
    two days trained me to ignore the digest entirely. Failures now appear as
    a section inside the one normal digest, and the subject line says how many
    sources were unread rather than shouting.
    """
    results = [
        SourceResult("Acme", "greenhouse", postings=[post(1)]),
        SourceResult("Beta", "lever", error="429 rate limited"),
    ]
    body = render_text([post(1)], results, {}, None, now=NOW)

    assert "1 of 2 sources read" in body
    assert "SOURCES NOT READ" in body
    assert "429 rate limited" in body
    assert "NEW ROLES" in body           # jobs and failures in ONE message
    for shouty in ("URGENT", "ALERT", "FAILED", "BROKEN"):
        assert shouty not in body

    subj = subject_line([post(1)], results)
    assert "1 new role" in subj
    assert "1 source(s) unread" in subj


def test_quiet_run_says_so_explicitly():
    """Zero results is the normal case and must not read as a malfunction."""
    results = [SourceResult("Acme", "greenhouse", postings=[post(1)])]
    body = render_text([], results, {}, None, now=NOW)
    assert "No new roles matched" in body
    assert "does not mean anything is broken" in body
    assert subject_line([], results) == "Job digest: No new roles"


def test_repeated_failure_gets_actionable_guidance():
    results = [SourceResult("Beta", "lever", error="404 -- slug wrong")]
    body = render_text([], results, {}, None, now=NOW)
    assert "several consecutive runs" in body
    assert "config.yaml" in body


def test_stats_section_flags_automated_screening():
    stats = Stats(total=40, awaiting=20, rejected=3, advanced=0, ghosted=17,
                  median_hours_to_rejection=31.0, fastest_rejection_hours=12.0,
                  slowest_rejection_hours=43.0, window_days=90)
    body = render_text([], [SourceResult("A", "lever", postings=[])],
                       {}, stats, now=NOW)
    assert "YOUR APPLICATIONS" in body
    assert "Median time to rejection: 31h" in body
    assert "automated" in body
    assert "eligibility and targeting" in body


def test_stats_section_omitted_when_empty():
    body = render_text([], [SourceResult("A", "lever", postings=[])],
                       {}, Stats(total=0), now=NOW)
    assert "YOUR APPLICATIONS" not in body


def test_filtered_histogram_rendered():
    body = render_text([], [SourceResult("A", "lever", postings=[])],
                       {"title excluded on 'product manager'": 12}, None, now=NOW)
    assert "FILTERED OUT" in body
    assert "12" in body
