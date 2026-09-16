"""Tests for application tracking, including the redaction boundary.

`test_public_stats_carry_no_identifiers` is the one worth reading. This
repository is public and the mailbox it reads is not, so the boundary between
private `Application` objects and publishable `Stats` needs to be a build
failure when broken, not a note in a docstring.
"""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pytest

from src.inbox import (
    Application,
    Stats,
    _company_from_domain,
    _sender_domain,
    classify,
    summarise,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def app(company: str, applied_h_ago: float, status: str = "awaiting",
        responded_h_ago: float | None = None, subject: str = "Your application"):
    return Application(
        message_id=f"<{company}@example.com>",
        company=company,
        subject=subject,
        applied_at=NOW - timedelta(hours=applied_h_ago),
        status=status,
        responded_at=None if responded_h_ago is None
        else NOW - timedelta(hours=responded_h_ago),
    )


# --------------------------------------------------------------------------- #
# The redaction boundary
# --------------------------------------------------------------------------- #

def test_public_stats_carry_no_identifiers():
    """No company name or subject line may survive into published stats.

    Uses distinctive sentinel strings and asserts none of them appear anywhere
    in the serialised public payload.
    """
    sentinels = ["ZZQXCorp", "WumpusLabs", "Fnordtastic"]
    apps = [
        app(sentinels[0], 100, "rejected", 69, subject="ZZQXCorp update"),
        app(sentinels[1], 80, "advanced", 40, subject="WumpusLabs next steps"),
        app(sentinels[2], 900),
    ]
    stats = summarise(apps, window_days=90, now=NOW)
    blob = json.dumps(stats.to_public_dict())

    for s in sentinels:
        assert s not in blob, f"{s} leaked into public stats"
    assert "application" not in blob.lower()


def test_stats_has_no_string_fields_at_all():
    """Structural guarantee: Stats cannot hold a name even by accident."""
    for f in fields(Stats):
        assert f.type not in ("str", "str | None"), (
            f"Stats.{f.name} is a string field. Stats is the published object; "
            "a string field there is a leak waiting to happen."
        )


def test_public_dict_is_whitelisted_not_reflected():
    """Adding a field must not auto-export it."""
    stats = summarise([app("Acme", 10)], window_days=30, now=NOW)
    stats.__dict__["secret_company"] = "Acme"  # simulate a careless future edit
    assert "secret_company" not in stats.to_public_dict()


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    "we have decided not to move forward with your application",
    "We will not be progressing your application at this time",
    "unfortunately we are pursuing other candidates",
    "You are no longer under consideration",
    "this position has been filled",
])
def test_rejections_classified(text):
    assert classify("Update on your application", text) == "rejected"


@pytest.mark.parametrize("text", [
    "Please schedule a call with our team",
    "We would like to speak with you",
    "Next steps: a technical screen",
    "We invite you to complete a take-home",
])
def test_advances_classified(text):
    assert classify("Your application", text) == "advanced"


def test_acknowledgement_saying_next_steps_is_not_an_advance():
    """The bug that reported a 60.8% response rate on a mailbox with no
    interviews in it. Application confirmations routinely say "next steps"."""
    for body in [
        "Thank you for applying. Here are the next steps in your application.",
        "We have received your application. Next steps: our team will review it.",
        "Thanks for applying to the role. What happens next: we'll be in touch.",
    ]:
        assert classify("Your application", body) == "awaiting", body


def test_genuine_invitation_still_counts_as_advance():
    for body in [
        "We would like to schedule a call with you this week",
        "Please book a slot with the hiring manager",
        "We invite you to complete a take-home exercise",
        "You have been selected for an interview",
        "We are moving you forward to the next round",
    ]:
        assert classify("Your application", body) == "advanced", body


def test_advance_wins_when_an_acknowledgement_also_invites():
    body = ("Thank you for applying. We'd like to schedule a call "
            "with you on Thursday.")
    assert classify("Your application", body) == "advanced"


def test_zero_hour_rejections_are_counted_not_averaged():
    """One row per company means a company whose only email is the rejection
    computes as 0h. Averaging those made the median look faster than reality."""
    apps = [
        app("A", 100, "rejected", 100),  # 0h — same message
        app("B", 100, "rejected", 69),   # 31h — real
        app("C", 100, "rejected", 57),   # 43h — real
    ]
    s = summarise(apps, 90, now=NOW)
    assert s.rejected == 3
    assert s.untimed_rejections == 1
    assert s.median_hours_to_rejection == 37.0   # median of 31 and 43 only
    assert s.fastest_rejection_hours == 31.0     # not 0.0
    assert "untimed_rejections" in s.to_public_dict()


def test_rejection_beats_advance_when_both_present():
    """Rejection emails routinely say 'next steps' in the sign-off.

    If ADVANCED won, every rejection mentioning next steps would be counted
    as progress -- flattering and useless.
    """
    body = ("We have decided not to move forward. "
            "For next steps, feel free to reapply in six months.")
    assert classify("Update", body) == "rejected"


def test_unrecognised_wording_is_awaiting_not_guessed():
    assert classify("Hello", "Just checking in about the role.") == "awaiting"


# --------------------------------------------------------------------------- #
# Ghosting and aggregate maths
# --------------------------------------------------------------------------- #

def test_old_awaiting_becomes_ghosted():
    apps = [app("Acme", 24 * 30), app("Beta", 24 * 2)]
    s = summarise(apps, 90, now=NOW)
    assert s.ghosted == 1
    assert s.awaiting == 1
    assert s.total == 2


def test_rejection_timing_stats():
    apps = [
        app("A", 100, "rejected", 69),   # 31h
        app("B", 100, "rejected", 88),   # 12h
        app("C", 100, "rejected", 57),   # 43h
    ]
    s = summarise(apps, 90, now=NOW)
    assert s.rejected == 3
    assert s.median_hours_to_rejection == 31.0
    assert s.fastest_rejection_hours == 12.0
    assert s.slowest_rejection_hours == 43.0


def test_response_rate_and_empty_case():
    s = summarise([app("A", 10, "rejected", 5), app("B", 10)], 90, now=NOW)
    assert s.response_rate == 50.0
    assert summarise([], 90, now=NOW).response_rate is None
    assert summarise([], 90, now=NOW).median_hours_to_rejection is None


# --------------------------------------------------------------------------- #
# Domain handling
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("Careers <no-reply@greenhouse.io>", "greenhouse.io"),
    ("jobs@acme.co.uk", "acme.co.uk"),
    ("garbage", ""),
])
def test_sender_domain(raw, expected):
    assert _sender_domain(raw) == expected


def test_company_name_skips_ats_vendor():
    """An email from acme.greenhouse.io is from Acme, not from Greenhouse."""
    assert _company_from_domain("acme.greenhouse.io") == "Acme"
    assert _company_from_domain("boards.lever.co") == "Boards"
