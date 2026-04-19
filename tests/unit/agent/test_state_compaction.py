from andera.agent.state import OBSERVATION_WINDOW, compact_observations


def test_under_window_unchanged():
    obs = [{"kind": "snapshot", "data": {"url": str(i)}} for i in range(3)]
    assert compact_observations(obs) == obs


def test_at_window_unchanged():
    obs = [{"kind": "snapshot", "data": {"url": str(i)}} for i in range(OBSERVATION_WINDOW)]
    assert compact_observations(obs) == obs


def test_over_window_compacts_head():
    obs = [{"kind": "snapshot", "data": {"url": str(i), "title": f"p{i}"}} for i in range(10)]
    compacted = compact_observations(obs)
    assert len(compacted) == 5 + OBSERVATION_WINDOW
    # first 5 become abstracts
    for a in compacted[:5]:
        assert a["kind"].endswith(".abstract")
        assert "summary" in a
    # last WINDOW preserved intact
    for orig, kept in zip(obs[-OBSERVATION_WINDOW:], compacted[-OBSERVATION_WINDOW:]):
        assert kept == orig


def test_extract_abstract_lists_fields():
    obs = (
        [{"kind": "snapshot", "data": {"url": "/x"}} for _ in range(OBSERVATION_WINDOW)]
        + [{"kind": "extract", "data": {"title": "t", "author": "a", "state": "open"}}]
    )
    # extract is newest; shouldn't be compacted. Push more in so it IS compacted:
    obs = obs + [{"kind": "snapshot", "data": {"url": "/y"}} for _ in range(OBSERVATION_WINDOW)]
    compacted = compact_observations(obs)
    first_extract_abstract = next(
        (a for a in compacted if a.get("kind") == "extract.abstract"), None
    )
    assert first_extract_abstract is not None
    assert "fields=" in first_extract_abstract["summary"]
