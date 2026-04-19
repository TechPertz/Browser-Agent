from andera.agent.plan_cache import PlanCache, plan_key


def test_url_pattern_normalizes_ids():
    k1 = plan_key("t", {"x": 1}, "https://linear.app/foo/issue/ENG-1")
    k2 = plan_key("t", {"x": 1}, "https://linear.app/foo/issue/ENG-2")
    k3 = plan_key("t", {"x": 1}, "https://linear.app/bar/issue/ENG-1")
    assert k1 == k2  # same pattern, different id
    assert k1 != k3  # different org segment


def test_schema_order_independence():
    a = plan_key("t", {"title": 1, "url": 2}, "x")
    b = plan_key("t", {"url": 2, "title": 1}, "x")
    assert a == b


def test_put_get_roundtrip(tmp_path):
    cache = PlanCache(tmp_path)
    key = plan_key("task", {"a": 1}, "https://x/1")
    assert cache.get(key) is None
    plan = [{"action": "goto", "target": "https://x"}]
    cache.put(key, plan)
    assert cache.get(key) == plan


def test_corrupt_file_returns_none(tmp_path):
    cache = PlanCache(tmp_path)
    key = plan_key("t", {}, None)
    (tmp_path / f"{key}.json").write_text("{{not json}}")
    assert cache.get(key) is None


def test_non_list_rejected(tmp_path):
    cache = PlanCache(tmp_path)
    key = plan_key("t", {}, None)
    (tmp_path / f"{key}.json").write_text('{"x": 1}')
    assert cache.get(key) is None
