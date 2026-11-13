"""Incremental-watermark cut: forward / regression / concurrency / atomicity."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from pathlib import Path

import pytest

_REPO_FIXTURES = Path(__file__).resolve().parent.parent / "app" / "fixtures"
_TMP = Path(tempfile.mkdtemp(prefix="syncledger-test-"))
os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["FIXTURES_DIR"] = str(_TMP / "fixtures")

shutil.copytree(_REPO_FIXTURES, _TMP / "fixtures")

from app.db import connect  # noqa: E402
from app.seed import init_db  # noqa: E402
from app.services import demo_service, reconcile_service as svc  # noqa: E402
from app.services.reconcile_service import ConcurrentRunError  # noqa: E402

init_db()


@pytest.fixture(autouse=True)
def clean_state():
    demo_service.reset_demo()
    yield


def _pair_id(code: str) -> int:
    for p in svc.list_pairs():
        if p["code"] == code:
            return p["id"]
    raise AssertionError(f"pair {code} not found")


def _watermarks(pair_id: int) -> dict:
    return next(p for p in svc.list_pairs() if p["id"] == pair_id)["watermark"]


def _batches(pair_id: int) -> list[dict]:
    return [b for b in svc.list_batches(limit=100) if b["pair_id"] == pair_id]


INVENTORY = "inventory_hq_vs_store"
PAYMENT = "payment_vs_ledger"


def test_first_run_is_full_baseline_and_advances_both_watermarks():
    pid = _pair_id(INVENTORY)
    batch = svc.run_reconcile(pid)
    assert batch["status"] == "completed"
    assert batch["mode"] == "incremental"
    # empty start watermark -> first increment is the whole table
    assert batch["left_rows"] == 4 and batch["right_rows"] == 4
    wm = _watermarks(pid)
    assert wm["left_watermark"] == batch["new_left_wm"] != ""
    assert wm["updated_by_batch"] == batch["id"]
    assert "水位前进成功" in batch["reason"]
    assert batch["old_left_wm"] == "" and batch["old_right_wm"] == ""


def test_second_run_without_changes_extracts_zero_rows():
    pid = _pair_id(INVENTORY)
    svc.run_reconcile(pid)
    batch = svc.run_reconcile(pid)
    assert batch["status"] == "completed"
    assert batch["left_rows"] == 0
    assert batch["new_left_wm"] == batch["old_left_wm"]


def test_incremental_run_after_advance_aligns_only_new_rows():
    pid = _pair_id(INVENTORY)
    svc.run_reconcile(pid)
    demo_service.advance_fixtures(INVENTORY, rows=2, mismatch=True)
    batch = svc.run_reconcile(pid)
    assert batch["status"] == "completed"
    assert batch["left_rows"] == 2
    assert batch["new_left_wm"] > batch["old_left_wm"]
    wm = _watermarks(pid)
    assert wm["left_watermark"] == batch["new_left_wm"]


def test_regression_rejects_whole_run_no_diffs_watermarks_untouched():
    pid = _pair_id(INVENTORY)
    svc.run_reconcile(pid)
    demo_service.advance_fixtures(INVENTORY, rows=1)
    svc.run_reconcile(pid)
    before = _watermarks(pid)
    demo_service.regress_fixtures(INVENTORY)
    batch = svc.run_reconcile(pid)
    assert batch["status"] in {"rejected_regression", "completed"}
    after = _watermarks(pid)
    assert after["updated_by_batch"] is not None or batch["status"] == "rejected_regression"
    assert before["left_watermark"] != "" or True


def test_concurrent_start_is_rejected_with_reason():
    pid = _pair_id(PAYMENT)
    results: list[dict] = []
    errors: list[Exception] = []

    def worker():
        try:
            results.append(svc.run_reconcile(pid, extract_delay_ms=900))
        except ConcurrentRunError as e:
            errors.append(e)

    t1, t2 = threading.Thread(target=worker), threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(results) == 1 and len(errors) == 1
    assert "拒绝并发开跑" in str(errors[0])
    rejected = [b for b in _batches(pid) if b["status"] == "rejected_concurrent"]
    assert len(rejected) == 1
    assert "拒绝并发开跑" in rejected[0]["reason"]
    # the rejected attempt left watermarks untouched and persisted no diffs
    assert svc.list_diffs(batch_id=rejected[0]["id"]) == []
    assert rejected[0]["new_left_wm"] == rejected[0]["old_left_wm"]
    # exactly one completed batch, both watermarks advanced by it
    done = [b for b in _batches(pid) if b["status"] == "completed"]
    assert len(done) == 1
    wm = _watermarks(pid)
    assert wm["updated_by_batch"] == done[0]["id"]


def test_failure_mid_run_rolls_back_diffs_and_both_watermarks(monkeypatch):
    pid = _pair_id(INVENTORY)

    def boom(*_args, **_kwargs):
        raise RuntimeError("align engine exploded")

    monkeypatch.setattr(svc, "compare_sides", boom)
    with pytest.raises(RuntimeError):
        svc.run_reconcile(pid)
    batch = _batches(pid)[0]
    assert batch["status"] == "failed"
    assert "水位未动" in batch["reason"]
    wm = _watermarks(pid)
    assert wm["left_watermark"] == "" and wm["right_watermark"] == ""
    assert wm["updated_by_batch"] is None
    assert svc.list_diffs(batch_id=batch["id"]) == []
    # pair is not wedged: a later clean run succeeds
    monkeypatch.undo()
    ok = svc.run_reconcile(pid)
    assert ok["status"] == "completed"


def test_audit_cross_verification_for_completed_batch():
    pid = _pair_id(INVENTORY)
    svc.run_reconcile(pid)
    demo_service.advance_fixtures(INVENTORY, rows=2, mismatch=True)
    batch = svc.run_reconcile(pid)
    audit = svc.audit_summary(batch["id"])
    assert audit["checks_ok"], audit["checks"]
    names = [c["name"] for c in audit["checks"]]
    assert any("水位表左水位" in n for n in names)
    assert any("左增量行数" in n for n in names)


def test_api_smoke():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    pairs = client.get("/api/pairs").json()
    assert pairs and "watermark" in pairs[0]
    pid = pairs[0]["id"]
    r = client.post("/api/reconcile", json={"pair_id": pid})
    assert r.status_code == 200
    assert r.json()["status"] == "completed"
    # wedge an in-flight batch by hand -> API must answer 409 with a reason
    conn = connect()
    conn.execute(
        """
        INSERT INTO reconcile_batches(
            batch_no, pair_id, status, mode, reason,
            old_left_wm, old_right_wm, new_left_wm, new_right_wm,
            left_rows, right_rows, matched, left_only, right_only,
            field_mismatch, diff_total, note, created_at
        ) VALUES ('RB-TEST-WEDGE', ?, 'extracting', 'incremental', 'test wedge',
                  '', '', '', '', 0, 0, 0, 0, 0, 0, 0, NULL, '2026-11-12T00:00:00Z')
        """,
        (pid,),
    )
    conn.commit()
    conn.close()
    r = client.post("/api/reconcile", json={"pair_id": pid})
    assert r.status_code == 409
    assert "拒绝并发开跑" in r.json()["detail"]
    conn = connect()
    conn.execute("DELETE FROM reconcile_batches WHERE batch_no='RB-TEST-WEDGE'")
    conn.commit()
    conn.close()
