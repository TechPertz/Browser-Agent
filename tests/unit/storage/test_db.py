from andera.storage import connect, init_db


def test_init_creates_expected_tables(tmp_path):
    db = init_db(tmp_path / "state.db")
    assert db.exists()
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r["name"] for r in rows}
    expected = {"runs", "samples", "artifacts", "queue", "audit_log", "event_log"}
    assert expected.issubset(names)


def test_init_is_idempotent(tmp_path):
    db_path = tmp_path / "state.db"
    init_db(db_path)
    init_db(db_path)  # must not raise
    assert db_path.exists()


def test_foreign_keys_enforced(tmp_path):
    db = init_db(tmp_path / "state.db")
    with connect(db) as conn:
        (fk,) = conn.execute("PRAGMA foreign_keys").fetchone()
        assert fk == 1
