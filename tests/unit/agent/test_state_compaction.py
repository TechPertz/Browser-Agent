"""Observation compaction preserves extract entries verbatim.

This is an accuracy-critical invariant: list_iter flows accumulate
extracted per-item data via `kind=="extract"` entries, and the
extractor node reads them directly. Abstracting these into 1-line
summaries silently dropped per-item data on long iterations.
"""

from andera.agent.state import OBSERVATION_WINDOW, compact_observations


def test_under_window_unchanged():
    obs = [{"kind": "snapshot", "data": {"url": str(i)}} for i in range(3)]
    assert compact_observations(obs) == obs


def test_at_window_unchanged():
    obs = [{"kind": "snapshot", "data": {"url": str(i)}} for i in range(OBSERVATION_WINDOW)]
    assert compact_observations(obs) == obs


def test_over_window_compacts_older_snapshots():
    obs = [{"kind": "snapshot", "data": {"url": str(i), "title": f"p{i}"}} for i in range(10)]
    compacted = compact_observations(obs)
    assert len(compacted) == 5 + OBSERVATION_WINDOW
    abstracts = [c for c in compacted if c["kind"].endswith(".abstract")]
    assert len(abstracts) == 5
    # last WINDOW snapshots preserved intact
    tail = [c for c in compacted if c["kind"] == "snapshot"]
    assert tail == obs[-OBSERVATION_WINDOW:]


def test_extract_observations_never_compacted():
    """Critical: a long run with many extract entries must preserve each one.

    Fixes a silent data-loss bug where list_iter specialists (iterating
    a list of items) accumulated per-item extracts but had all but the
    last 5 replaced with 1-line summaries before the extractor node
    aggregated them.
    """
    extracts = [
        {"kind": "extract", "data": {"row": i, "title": f"item-{i}"}}
        for i in range(12)
    ]
    snapshots = [
        {"kind": "snapshot", "data": {"url": f"/x/{i}"}} for i in range(12)
    ]
    # Interleave extract + snapshot, then compact.
    interleaved: list[dict] = []
    for a, b in zip(extracts, snapshots):
        interleaved.append(a)
        interleaved.append(b)
    compacted = compact_observations(interleaved)
    # Every extract entry must be present verbatim.
    kept_extracts = [c for c in compacted if c.get("kind") == "extract"]
    assert len(kept_extracts) == len(extracts)
    for orig in extracts:
        assert orig in kept_extracts


def test_extract_preserved_even_when_snapshots_exceed_window():
    obs = (
        [{"kind": "extract", "data": {"row": i}} for i in range(3)]
        + [{"kind": "snapshot", "data": {"url": f"/p/{i}"}} for i in range(20)]
    )
    compacted = compact_observations(obs)
    # All 3 extracts survive
    assert sum(1 for c in compacted if c.get("kind") == "extract") == 3
    # Only last OBSERVATION_WINDOW snapshots survive; older are abstracted
    assert sum(1 for c in compacted if c.get("kind") == "snapshot") == OBSERVATION_WINDOW
    assert sum(1 for c in compacted if c.get("kind") == "snapshot.abstract") == 20 - OBSERVATION_WINDOW
