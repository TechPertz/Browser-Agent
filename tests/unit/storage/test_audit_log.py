import sqlite3

from andera.storage.audit_log import GENESIS_HASH, AuditLog


def test_first_append_has_genesis_prev(tmp_path):
    log = AuditLog(tmp_path / "a.db")
    h = log.append(kind="run.started", run_id="r1", payload={"x": 1})
    with sqlite3.connect(tmp_path / "a.db") as c:
        prev = c.execute("SELECT prev_hash FROM audit_log LIMIT 1").fetchone()[0]
    assert prev == GENESIS_HASH
    assert len(h) == 64


def test_chain_prev_equals_previous_this(tmp_path):
    log = AuditLog(tmp_path / "a.db")
    h1 = log.append(kind="run.started", run_id="r1")
    h2 = log.append(kind="sample.started", run_id="r1", sample_id="s1")
    with sqlite3.connect(tmp_path / "a.db") as c:
        rows = c.execute(
            "SELECT prev_hash, this_hash FROM audit_log ORDER BY rowid ASC"
        ).fetchall()
    assert rows[1][0] == h1
    assert rows[1][1] == h2


def test_verify_chain_passes_on_clean(tmp_path):
    log = AuditLog(tmp_path / "a.db")
    for i in range(5):
        log.append(kind="tool.called", run_id="r1", sample_id=f"s{i}", payload={"i": i})
    assert log.verify_chain() is True


def test_verify_chain_detects_tamper(tmp_path):
    log = AuditLog(tmp_path / "a.db")
    for i in range(3):
        log.append(kind="tool.called", run_id="r1", payload={"i": i})
    # Tamper: flip one payload row without updating hash.
    with sqlite3.connect(tmp_path / "a.db") as c:
        c.execute(
            "UPDATE audit_log SET payload_json='{\"i\":99}' WHERE rowid=2"
        )
    assert log.verify_chain() is False


def test_root_hash_scoped_to_run(tmp_path):
    log = AuditLog(tmp_path / "a.db")
    log.append(kind="run.started", run_id="r1")
    log.append(kind="run.started", run_id="r2")
    r1 = log.root_hash("r1")
    r2 = log.root_hash("r2")
    assert r1 != r2
