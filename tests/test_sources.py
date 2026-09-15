"""Adapter tests against fixtures, plus the failure-handling contract.

No test in this file touches the network. The adapters are pure functions over
JSON, so the fixtures below are the whole surface. The live endpoints are
smoke-tested separately by the `smoke` job in the Actions workflow, where a
failure is a warning rather than a broken build -- a third party changing their
API should not turn every commit red.
"""

from __future__ import annotations

from datetime import timezone

import pytest

from src.sources import (
    ADAPTERS,
    PermanentSourceError,
    SourceResult,
    fetch_board,
    parse_ashby,
    parse_greenhouse,
    parse_lever,
)

GREENHOUSE = {
    "jobs": [
        {
            "id": 4012345,
            "title": "Senior Product Designer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/4012345",
            "location": {"name": "Remote - EMEA"},
            "updated_at": "2026-09-10T09:30:00-04:00",
        },
        {
            "id": 4012346,
            "title": "Product Manager",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/4012346",
            "location": {"name": "New York, NY"},
            "updated_at": "2026-09-11T09:30:00-04:00",
        },
    ]
}

LEVER = [
    {
        "id": "b1c2d3e4-0000-4444-8888-aaaabbbbcccc",
        "text": "Product Designer",
        "hostedUrl": "https://jobs.lever.co/acme/b1c2d3e4",
        "categories": {"location": "Remote", "team": "Design",
                       "commitment": "Full-time"},
        "createdAt": 1789032600000,  # 2026-09-10T09:30:00Z in epoch millis
    }
]

ASHBY = {
    "apiVersion": "1",
    "jobs": [
        {
            "id": "11112222-3333-4444-5555-666677778888",
            "title": "UX Designer",
            "location": "Dubai, UAE",
            "jobUrl": "https://jobs.ashbyhq.com/acme/1111",
            "isListed": True,
            "publishedAt": "2026-09-12T00:00:00Z",
        },
        {
            "id": "99990000-3333-4444-5555-666677778888",
            "title": "Hidden Draft Role",
            "location": "Remote",
            "jobUrl": "https://jobs.ashbyhq.com/acme/9999",
            "isListed": False,
            "publishedAt": "2026-09-12T00:00:00Z",
        },
    ],
}


def test_greenhouse_adapter():
    out = parse_greenhouse("Acme", GREENHOUSE)
    assert len(out) == 2
    p = out[0]
    assert p.uid == "greenhouse:Acme:4012345"
    assert p.title == "Senior Product Designer"
    assert p.location == "Remote - EMEA"
    assert p.posted_at.astimezone(timezone.utc).day == 10


def test_lever_adapter_reads_epoch_millis():
    out = parse_lever("Acme", LEVER)
    assert len(out) == 1
    assert out[0].title == "Product Designer"
    assert out[0].location == "Remote"
    assert out[0].posted_at is not None
    # Lever hands out epoch milliseconds, not ISO strings. Dividing by 1000 is
    # the whole fix, and getting it wrong shifts every date by ~55 years.
    assert out[0].posted_at.year == 2026
    assert out[0].posted_at.month == 9
    assert out[0].posted_at.day == 10


def test_ashby_adapter_drops_unlisted():
    """isListed: false means the posting is not public. Including it would
    send me a link that 404s, which is worse than not sending anything."""
    out = parse_ashby("Acme", ASHBY)
    assert len(out) == 1
    assert out[0].title == "UX Designer"


def test_uids_are_stable_and_namespaced():
    """Two vendors can hand out the same native id. The uid must not collide,
    because a collision silently hides a real job."""
    a = parse_greenhouse("Acme", GREENHOUSE)[0].uid
    b = parse_lever("Acme", LEVER)[0].uid
    assert a != b
    assert a.startswith("greenhouse:") and b.startswith("lever:")
    assert parse_greenhouse("Acme", GREENHOUSE)[0].uid == a  # deterministic


def test_empty_and_malformed_payloads_do_not_raise():
    assert parse_greenhouse("Acme", {}) == []
    assert parse_greenhouse("Acme", None) == []
    assert parse_lever("Acme", []) == []
    assert parse_ashby("Acme", {"jobs": []}) == []


def test_bad_date_becomes_none_rather_than_crashing():
    payload = {"jobs": [{"id": 1, "title": "X", "absolute_url": "u",
                         "location": {"name": "Y"}, "updated_at": "not-a-date"}]}
    assert parse_greenhouse("Acme", payload)[0].posted_at is None


# --------------------------------------------------------------------------- #
# The failure contract: fetch_board must never raise, and must never notify.
# --------------------------------------------------------------------------- #

def test_unknown_ats_is_an_error_result_not_an_exception():
    r = fetch_board("Acme", "workday", "acme")
    assert isinstance(r, SourceResult)
    assert not r.ok
    assert "unknown ats" in r.error


def test_network_failure_becomes_an_error_result(monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("connection timed out")
    monkeypatch.setattr("src.sources._get_json", boom)
    r = fetch_board("Acme", "greenhouse", "acme")
    assert not r.ok
    assert "timed out" in r.error
    assert r.postings == []


def test_parse_failure_becomes_an_error_result(monkeypatch):
    monkeypatch.setattr("src.sources._get_json", lambda *a, **k: {"jobs": [{}]})
    r = fetch_board("Acme", "greenhouse", "acme")
    assert not r.ok
    assert "parse failed" in r.error


def test_all_three_vendors_are_registered():
    assert set(ADAPTERS) == {"greenhouse", "lever", "ashby"}
    for template, _ in ADAPTERS.values():
        assert "{slug}" in template
        assert template.startswith("https://")
