"""Descriptor matcher tests — the core of cross-row Set-of-Mark replay.

A cached plan from sample 1 (facebook/react) must resolve correctly on
sample 2 (microsoft/vscode) even though PR titles, authors, and counts
all differ. These tests assert the structural-descriptor invariant:
content changes, structure does not.
"""

from __future__ import annotations

from andera.agent.nodes import (
    _descriptor_for,
    _filter_by_descriptor,
    _match_descriptor,
)


def _mk(mark_id, **kw):
    """Shorthand for building a mark dict with defaults."""
    return {
        "mark_id": mark_id,
        "role": kw.get("role", "a"),
        "name": kw.get("name", ""),
        "href": kw.get("href", ""),
        "placeholder": kw.get("placeholder", ""),
        "viewport_region": kw.get("viewport_region", ""),
    }


def test_href_pattern_matches_across_repos():
    """The critical cross-row test. Descriptor captured on repo A must
    pick the equivalent link on repo B."""
    desc = {
        "role": "a",
        "href_pattern": r"/pull/\d+",
        "ordinal": 0,
    }
    # Sample 1 marks — facebook/react
    marks_a = [
        _mk(0, role="a", href="/facebook/react/pulls", name="Pull requests"),
        _mk(1, role="a", href="/facebook/react/pull/9876", name="Add hooks support"),
        _mk(2, role="a", href="/facebook/react/pull/9875", name="Fix memo bug"),
    ]
    hit_a = _match_descriptor(desc, marks_a)
    assert hit_a["mark_id"] == 1  # first PR link on react

    # Sample 2 marks — microsoft/vscode, totally different PR titles
    marks_b = [
        _mk(0, role="a", href="/microsoft/vscode/pulls", name="Pull requests"),
        _mk(1, role="a", href="/microsoft/vscode/pull/200001", name="Refactor telemetry"),
        _mk(2, role="a", href="/microsoft/vscode/pull/199999", name="Fix terminal crash"),
    ]
    hit_b = _match_descriptor(desc, marks_b)
    assert hit_b["mark_id"] == 1  # same structural position, different repo


def test_ordinal_picks_nth_among_candidates():
    desc = {"role": "a", "href_pattern": r"/pull/\d+", "ordinal": 2}
    marks = [
        _mk(5, role="a", href="/repo/pull/100"),
        _mk(6, role="a", href="/repo/pull/101"),
        _mk(7, role="a", href="/repo/pull/102"),
    ]
    assert _match_descriptor(desc, marks)["mark_id"] == 7


def test_missing_element_returns_none_triggering_vision_fallback():
    """When the page no longer has a matching element (site variant),
    the matcher returns None so act can fall back to vision."""
    desc = {"role": "button", "name_pattern": "^Sign in$", "ordinal": 0}
    marks = [_mk(0, role="a", href="/login", name="Log in")]  # site renamed the CTA
    assert _match_descriptor(desc, marks) is None


def test_name_pattern_works_for_branded_ctas():
    """Some labels are stable across site variants (Google Search,
    Sign In). Vision can use name_pattern for those."""
    desc = {"role": "button", "name_pattern": r"(?i)sign\s?in", "ordinal": 0}
    marks = [
        _mk(0, role="button", name="Home"),
        _mk(1, role="button", name="Sign In"),
        _mk(2, role="button", name="Menu"),
    ]
    assert _match_descriptor(desc, marks)["mark_id"] == 1


def test_placeholder_pattern_for_search_inputs():
    """Search boxes often have no label but a stable placeholder."""
    desc = {"role": "input", "placeholder_pattern": r"(?i)search"}
    marks = [
        _mk(0, role="input", placeholder="Email"),
        _mk(1, role="input", placeholder="Search repositories"),
    ]
    assert _match_descriptor(desc, marks)["mark_id"] == 1


def test_viewport_region_fallback():
    """Unlabeled icon buttons — 'the X in the top-right' works across
    sites even when the icon itself changes."""
    desc = {"role": "button", "viewport_region": "top-right"}
    marks = [
        _mk(0, role="button", viewport_region="top-left", name=""),
        _mk(1, role="button", viewport_region="top-right", name=""),
    ]
    assert _match_descriptor(desc, marks)["mark_id"] == 1


def test_bad_regex_from_vision_doesnt_poison_replay():
    """Vision occasionally emits malformed regex. The matcher treats
    that field as no-op rather than crashing the sample."""
    desc = {"role": "a", "href_pattern": r"[unclosed", "ordinal": 0}
    marks = [_mk(0, role="a", href="/pull/1"), _mk(1, role="a", href="/pull/2")]
    hit = _match_descriptor(desc, marks)
    # With the bad pattern treated as no-op, we still get the first <a>.
    assert hit["mark_id"] == 0


def test_descriptor_for_records_ordinal_among_siblings():
    """When the chosen mark is the 3rd matching link, descriptor.ordinal
    = 2 so replay picks the same one."""
    marks = [
        _mk(0, role="a", href="/pulls"),
        _mk(1, role="a", href="/repo/pull/1"),
        _mk(2, role="a", href="/repo/pull/2"),
        _mk(3, role="a", href="/repo/pull/3"),
    ]
    chosen = marks[3]
    hint = {"href_pattern": r"/pull/\d+"}
    desc = _descriptor_for(chosen, marks, hint)
    assert desc["role"] == "a"
    assert desc["href_pattern"] == r"/pull/\d+"
    assert desc["ordinal"] == 2  # third PR link


def test_descriptor_for_without_vision_hint_falls_back_to_role():
    """If vision returns no structural hint (weird edge), descriptor
    still works — role + ordinal-among-role-matches."""
    marks = [
        _mk(0, role="button", name="A"),
        _mk(1, role="a", name="B"),
        _mk(2, role="button", name="C"),
    ]
    desc = _descriptor_for(marks[2], marks, None)
    assert desc["role"] == "button"
    assert desc["ordinal"] == 1  # second button


def test_returns_none_when_ordinal_out_of_range():
    """Page changed — used to have 3 PR links, now has 1. Descriptor
    for ordinal=2 gracefully returns None → triggers vision."""
    desc = {"role": "a", "href_pattern": r"/pull/\d+", "ordinal": 2}
    marks = [_mk(0, role="a", href="/foo/pull/1")]
    assert _match_descriptor(desc, marks) is None


def test_filter_is_and_not_or():
    """Multiple descriptor fields narrow — matches must satisfy ALL."""
    desc = {"role": "a", "href_pattern": r"/pull/", "viewport_region": "main"}
    marks = [
        _mk(0, role="a", href="/pull/1", viewport_region="header"),  # wrong region
        _mk(1, role="a", href="/pulls", viewport_region="main"),     # wrong href
        _mk(2, role="a", href="/pull/2", viewport_region="main"),    # match
    ]
    hits = _filter_by_descriptor(marks, desc)
    assert [m["mark_id"] for m in hits] == [2]


def test_empty_descriptor_field_is_noop_not_reject_all():
    """Descriptor only sets role — other fields empty should not filter
    anything out."""
    desc = {"role": "a"}
    marks = [_mk(0, role="a"), _mk(1, role="a", href="/x"), _mk(2, role="button")]
    hits = _filter_by_descriptor(marks, desc)
    assert [m["mark_id"] for m in hits] == [0, 1]
