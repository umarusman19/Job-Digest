"""Filter tests. The word-boundary cases are the ones that caught real bugs."""

from __future__ import annotations

import pytest

from src.filters import Rules, apply_rules, judge
from src.sources import Posting


def post(title: str, location: str = "Remote") -> Posting:
    return Posting(uid=f"t:{title}", company="Acme", title=title,
                   location=location, url="u", ats="greenhouse")


RULES = Rules(
    title_include=["product designer", "ux designer", "design engineer"],
    title_exclude=["product manager", "intern", "director", "vp", "ui"],
    location_allow=["remote", "pakistan", "dubai", "emea"],
    location_deny=["us only", "pst"],
)


def test_keeps_a_matching_role():
    assert judge(post("Senior Product Designer"), RULES).keep


def test_excludes_before_including():
    """'Product Manager, Design Systems' contains neither include term, but
    the important case is a title containing both: exclusion must win."""
    v = judge(post("Product Designer / Product Manager hybrid"), RULES)
    assert not v.keep
    assert "product manager" in v.reason


def test_word_boundaries_ui_does_not_match_building():
    """'ui' is in the exclude list. It must not match 'building' or 'guide'.

    This is the bug that made an earlier version drop almost everything: a
    naive substring check excluded 'Designer, Building Systems'.
    """
    assert judge(post("UX Designer, Building Systems"), RULES).keep
    assert not judge(post("UI Designer"), RULES).keep


def test_word_boundaries_pst_does_not_match_gypsum():
    rules = Rules(title_include=[], title_exclude=[],
                  location_allow=[], location_deny=["pst"])
    assert judge(post("X", "Gypsumville, Canada"), rules).keep
    assert not judge(post("X", "Remote (PST)"), rules).keep


def test_location_deny_beats_allow():
    v = judge(post("Product Designer", "Remote - US only"), RULES)
    assert not v.keep
    assert "denied" in v.reason


def test_unmatched_location_dropped_when_required():
    assert not judge(post("Product Designer", "Tokyo, Japan"), RULES).keep


def test_unmatched_location_kept_when_not_required():
    relaxed = Rules(
        title_include=RULES.title_include, title_exclude=RULES.title_exclude,
        location_allow=RULES.location_allow, location_deny=RULES.location_deny,
        require_location_match=False,
    )
    assert judge(post("Product Designer", "Tokyo, Japan"), relaxed).keep


def test_empty_include_list_keeps_everything_not_excluded():
    rules = Rules(title_include=[], title_exclude=["intern"],
                  location_allow=[], location_deny=[])
    assert judge(post("Anything At All"), rules).keep
    assert not judge(post("Design Intern"), rules).keep


def test_drop_histogram_explains_itself():
    """A filter that drops everything must look different from a quiet week."""
    postings = [
        post("Product Designer"),
        post("Product Manager"),
        post("Design Intern"),
        post("Product Designer", "Tokyo"),
    ]
    kept, dropped = apply_rules(postings, RULES)
    assert len(kept) == 1
    assert sum(dropped.values()) == 3
    assert any("product manager" in r for r in dropped)
    assert any("location" in r for r in dropped)


def test_rules_from_config_lowercases():
    rules = Rules.from_config({"filters": {
        "title_include": ["Product Designer"],
        "location_allow": ["REMOTE"],
    }})
    assert rules.title_include == ["product designer"]
    assert rules.location_allow == ["remote"]
    assert rules.require_location_match is True
