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
    assert batch["right_rows"] == 2
    assert batch["new_left_wm"] > batch["old_left_wm"]
    wm = _watermarks(pid)
    # BOTH sides must advance in the same batch — the historical bug left the
    # right side at its old value (or empty), forcing a full re-extract next run.
    assert wm["left_watermark"] == batch["new_left_wm"]
    assert wm["right_watermark"] == batch["new_right_wm"]
    assert wm["right_watermark"] != ""
    assert wm["updated_by_batch"] == batch["id"]
    # Next run over an unchanged source must extract nothing on either side.
    again = svc.run_reconcile(pid)
    assert again["status"] == "completed"
    assert again["left_rows"] == 0 and again["right_rows"] == 0
    assert again["new_left_wm"] == again["old_left_wm"]
    assert again["new_right_wm"] == again["old_right_wm"]


def _advance_then_regress(side: str) -> tuple[dict, dict, dict]:
    pid = _pair_id(INVENTORY)
    svc.run_reconcile(pid)
    demo_service.advance_fixtures(INVENTORY, rows=2, mismatch=True)
    svc.run_reconcile(pid)
    before = dict(_watermarks(pid))
    demo_service.regress_fixtures(INVENTORY, side=side)
    batch = svc.run_reconcile(pid)
    after = _watermarks(pid)
    return before, batch, after


def test_regression_both_sides_rejects_whole_run_no_diffs_watermarks_untouched():
    before, batch, after = _advance_then_regress("both")
    assert before["left_watermark"] and before["right_watermark"]
    assert batch["status"] == "rejected_regression"
    assert "整趟拒绝" in batch["reason"]
    assert svc.list_diffs(batch_id=batch["id"]) == []
    assert batch["new_left_wm"] == batch["old_left_wm"]
    assert batch["new_right_wm"] == batch["old_right_wm"]
    assert after == before  # neither watermark row field moved


def test_left_only_rollback_rejects_whole_run_and_right_watermark_does_not_move():
    before, batch, after = _advance_then_regress("left")
    assert batch["status"] == "rejected_regression"
    assert "左端水位回退" in batch["reason"]
    assert "右端水位回退" not in batch["reason"]
    assert svc.list_diffs(batch_id=batch["id"]) == []
    # The right side had NOT rolled back, yet the whole run is rejected and
    # the right watermark must stay exactly where it was (not move at all).
    assert after["right_watermark"] == before["right_watermark"]
    assert after["left_watermark"] == before["left_watermark"]
    assert after["updated_by_batch"] == before["updated_by_batch"]
    assert batch["new_left_wm"] == batch["old_left_wm"] == before["left_watermark"]
    assert batch["new_right_wm"] == batch["old_right_wm"] == before["right_watermark"]
    # Audit must confirm zero diffs, both sides untouched, batch not recorded
    # as the watermark-advancing batch.
    audit = svc.audit_summary(batch["id"])
    assert audit["checks_ok"], audit["checks"]


def test_right_only_rollback_rejects_whole_run_and_left_watermark_does_not_move():
    before, batch, after = _advance_then_regress("right")
    assert batch["status"] == "rejected_regression"
    assert "右端水位回退" in batch["reason"]
    assert "左端水位回退" not in batch["reason"]
    assert svc.list_diffs(batch_id=batch["id"]) == []
    assert after["left_watermark"] == before["left_watermark"]
    assert after["right_watermark"] == before["right_watermark"]
    assert after["updated_by_batch"] == before["updated_by_batch"]
    audit = svc.audit_summary(batch["id"])
    assert audit["checks_ok"], audit["checks"]


def test_completed_batch_watermarks_and_report_commit_together(monkeypatch):
    """A failure after the batch row is written must not leave either watermark
    ahead of a batch that never completed (commit tear)."""
    pid = _pair_id(INVENTORY)

    import app.repositories.watermarks as wm_repo

    def boom(*_args, **_kwargs):
        raise RuntimeError("watermark write exploded")

    monkeypatch.setattr(wm_repo, "advance", boom)
    with pytest.raises(RuntimeError):
        svc.run_reconcile(pid)
    batch = _batches(pid)[0]
    assert batch["status"] == "failed"
    wm = _watermarks(pid)
    assert wm["left_watermark"] == "" and wm["right_watermark"] == ""
    assert wm["updated_by_batch"] is None
    assert svc.list_diffs(batch_id=batch["id"]) == []
    monkeypatch.undo()
    ok = svc.run_reconcile(pid)
    assert ok["status"] == "completed"
    assert _watermarks(pid)["updated_by_batch"] == ok["id"]


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
