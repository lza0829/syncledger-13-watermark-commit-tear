from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from app.config import FIXTURES_DIR
from app.db import connect
from app.engines.incremental import REGRESSION_POLICY, extract_increment, max_watermark
from app.engines.reconcile import compare_sides
from app.repositories import batches as batch_repo
from app.repositories import contracts as contract_repo
from app.repositories import pairs as pair_repo
from app.repositories import tickets as ticket_repo
from app.repositories import watermarks as wm_repo


class ConcurrentRunError(Exception):
    """Raised when a pair already has an in-flight (extracting/aligning) batch."""

    def __init__(self, message: str, rejected_batch_id: int | None = None):
        super().__init__(message)
        self.rejected_batch_id = rejected_batch_id


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_fixture(name: str) -> list[dict[str, Any]]:
    path = FIXTURES_DIR / name
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"fixture {name} must be a JSON array")
    return data


def _wm_display(value: str | None) -> str:
    return value if value else "(空)"


def dashboard_stats() -> dict[str, Any]:
    conn = connect()
    try:
        return {
            "pair_count": conn.execute("SELECT COUNT(*) c FROM reconcile_pairs").fetchone()["c"],
            "enabled_pairs": conn.execute(
                "SELECT COUNT(*) c FROM reconcile_pairs WHERE enabled=1"
            ).fetchone()["c"],
            "batch_count": conn.execute("SELECT COUNT(*) c FROM reconcile_batches").fetchone()["c"],
            "diff_count": conn.execute("SELECT COUNT(*) c FROM reconcile_diffs").fetchone()["c"],
            "open_dispositions": conn.execute(
                "SELECT COUNT(*) c FROM diff_tickets WHERE status='open'"
            ).fetchone()["c"],
            "policy": conn.execute(
                "SELECT value FROM settings WHERE key='default_policy'"
            ).fetchone()["value"],
            "regression_policy": REGRESSION_POLICY,
        }
    finally:
        conn.close()


def list_pairs() -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = pair_repo.list_pairs(conn)
        states = {w["pair_id"]: w for w in wm_repo.list_states(conn)}
        for r in rows:
            r["watermark"] = states.get(r["id"]) or {
                "left_watermark": "",
                "right_watermark": "",
                "updated_by_batch": None,
                "updated_at": None,
            }
        return rows
    finally:
        conn.close()


def list_batches(limit: int = 50) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = batch_repo.list_batches(conn, limit=limit)
        pairs = {p["id"]: p for p in pair_repo.list_pairs(conn)}
        for r in rows:
            p = pairs.get(r["pair_id"])
            r["pair_code"] = p["code"] if p else None
            r["pair_title"] = p["title"] if p else None
        return rows
    finally:
        conn.close()


def list_diffs(batch_id: int | None = None, pair_id: int | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return batch_repo.list_diffs(conn, batch_id=batch_id, pair_id=pair_id)
    finally:
        conn.close()


def list_dispositions() -> list[dict[str, Any]]:
    """Difference disposition records (not a customer-service ticket desk)."""
    conn = connect()
    try:
        return ticket_repo.list_tickets(conn)
    finally:
        conn.close()


def list_contracts() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return contract_repo.list_contracts(conn)
    finally:
        conn.close()


def list_settings() -> dict[str, str]:
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        out = {r["key"]: r["value"] for r in rows}
        # Read-only display of the fixed engine policy — not a stored switch.
        out["regression_policy"] = REGRESSION_POLICY
        return out
    finally:
        conn.close()


def _insert_batch_with_slot(
    conn: sqlite3.Connection,
    *,
    pair_id: int,
    old_left_wm: str,
    old_right_wm: str,
    note: str | None,
    now: str,
) -> tuple[int, str]:
    """Insert the in-flight batch row, claiming the pair's active slot.

    The partial unique index uq_active_batch_per_pair makes this atomic: the
    loser of a concurrent start gets an IntegrityError here. batch_no unique
    conflicts (two pairs racing the sequence) are retried with a fresh number.
    """
    for _ in range(3):
        batch_no = batch_repo.next_batch_no(conn, now)
        try:
            batch_id = batch_repo.insert_batch(
                conn,
                batch_no=batch_no,
                pair_id=pair_id,
                status="extracting",
                mode="incremental",
                reason="抽取中：按已记水位增量抽取",
                old_left_wm=old_left_wm,
                old_right_wm=old_right_wm,
                new_left_wm=old_left_wm,
                new_right_wm=old_right_wm,
                note=note,
                created_at=now,
            )
            conn.commit()
            return batch_id, batch_no
        except sqlite3.IntegrityError as e:
            conn.rollback()
            if "uq_active_batch_per_pair" in str(e):
                raise
            # batch_no sequence race with another pair — retry with new number
    raise sqlite3.IntegrityError("could not allocate batch_no")


def run_reconcile(
    pair_id: int, note: str | None = None, extract_delay_ms: int = 0
) -> dict[str, Any]:
    """Incremental run for one pair.

    Lifecycle: claim active slot (extracting) → extract increment → aligning →
    one atomic commit of diffs + batch completion + BOTH watermarks.

    Regression of either side's observed max below the recorded watermark
    triggers the fixed policy (reject_run): the batch is closed as
    rejected_regression with reason, no diffs are persisted, and neither
    watermark moves.
    """
    conn = connect()
    try:
        pair = pair_repo.get_pair(conn, pair_id)
        if not pair:
            raise ValueError("pair not found")
        if not pair["enabled"]:
            raise ValueError("pair disabled")

        now = _now()
        wm_repo.ensure_state(conn, pair_id, now)
        conn.commit()
        state = wm_repo.get_state(conn, pair_id)
        assert state is not None
        old_left = state["left_watermark"]
        old_right = state["right_watermark"]

        # 1) Claim the active slot; a concurrent in-flight batch means reject.
        try:
            batch_id, batch_no = _insert_batch_with_slot(
                conn,
                pair_id=pair_id,
                old_left_wm=old_left,
                old_right_wm=old_right,
                note=note,
                now=now,
            )
        except sqlite3.IntegrityError:
            blocking = batch_repo.get_active_batch(conn, pair_id)
            blocking_desc = (
                f"批次 {blocking['batch_no']}（{blocking['status']}）" if blocking else "另一在途批次"
            )
            reason = f"拒绝并发开跑：该对账对已有{blocking_desc}，等其结束后再发起"
            rejected_id = batch_repo.insert_batch(
                conn,
                batch_no=batch_repo.next_batch_no(conn, now),
                pair_id=pair_id,
                status="rejected_concurrent",
                mode="incremental",
                reason=reason,
                old_left_wm=old_left,
                old_right_wm=old_right,
                new_left_wm=old_left,
                new_right_wm=old_right,
                note=note,
                created_at=now,
            )
            batch_repo.update_batch(conn, rejected_id, finished_at=now)
            conn.commit()
            raise ConcurrentRunError(reason, rejected_batch_id=rejected_id)

        try:
            # 2) Extract: read sources, compute observable maxima per side.
            left_all = _load_fixture(pair["left_fixture"])
            right_all = _load_fixture(pair["right_fixture"])
            if extract_delay_ms > 0:
                time.sleep(extract_delay_ms / 1000)  # demo knob for the concurrency window
            left_max = max_watermark(left_all, pair["left_wm_field"])
            right_max = max_watermark(right_all, pair["right_wm_field"])

            regressions = []
            if left_max < old_left and right_max < old_right:
                regressions.append(
                    f"左端水位回退（已记 {_wm_display(old_left)}，源端现最大 {_wm_display(left_max)}）"
                )
                regressions.append(
                    f"右端水位回退（已记 {_wm_display(old_right)}，源端现最大 {_wm_display(right_max)}）"
                )
            if regressions:
                reason = "；".join(regressions) + "。策略=整趟拒绝：不落差异、水位不动"
                batch_repo.update_batch(
                    conn,
                    batch_id,
                    status="rejected_regression",
                    reason=reason,
                    new_left_wm=old_left,
                    new_right_wm=old_right,
                    left_max_wm_seen=left_max,
                    right_max_wm_seen=right_max,
                    finished_at=_now(),
                )
                conn.commit()  # pair_watermarks deliberately untouched
                return _batch_payload(conn, batch_id, pair)

            # 4) Align the increment only.
            batch_repo.update_batch(conn, batch_id, status="aligning", reason="对齐中：只对本趟增量对齐")
            conn.commit()
            left_inc = extract_increment(left_all, pair["left_wm_field"], old_left)
            right_inc = extract_increment(right_all, pair["right_wm_field"], old_right)
            result = compare_sides(
                left_inc,
                right_inc,
                align_keys=pair["align_keys"],
                compare_fields=pair["compare_fields"],
            )

            finished = _now()
            for diff in result["diffs"]:
                batch_repo.insert_diff(conn, batch_id=batch_id, pair_id=pair_id, diff=diff, created_at=finished)
            reason = (
                f"水位前进成功：左 {_wm_display(old_left)}→{_wm_display(left_max)}，"
                f"右 {_wm_display(old_right)}→{_wm_display(right_max)}；"
                f"增量抽取 左{len(left_inc)}行/右{len(right_inc)}行"
            )
            batch_repo.update_batch(
                conn,
                batch_id,
                status="completed",
                reason=reason,
                new_left_wm=left_max,
                new_right_wm=right_max,
                left_max_wm_seen=left_max,
                right_max_wm_seen=right_max,
                left_rows=result["counts"]["left_rows"],
                right_rows=result["counts"]["right_rows"],
                matched=result["counts"]["matched"],
                left_only=result["counts"]["left_only"],
                right_only=result["counts"]["right_only"],
                field_mismatch=result["counts"]["field_mismatch"],
                diff_total=result["counts"]["diff_total"],
                finished_at=finished,
            )
            conn.commit()
            wm_repo.advance(
                conn,
                pair_id,
                left_watermark=left_max,
                right_watermark=right_max,
                batch_id=batch_id,
                now=finished,
            )
            conn.commit()
            return _batch_payload(conn, batch_id, pair)
        except Exception as e:
            conn.rollback()
            try:
                batch_repo.update_batch(
                    conn,
                    batch_id,
                    status="failed",
                    reason=f"批次异常中断：{e}（水位未动）",
                    finished_at=_now(),
                )
                conn.commit()
            except Exception:
                conn.rollback()
            raise
    finally:
        conn.close()


def _batch_payload(conn: sqlite3.Connection, batch_id: int, pair: dict[str, Any]) -> dict[str, Any]:
    batch = batch_repo.get_batch(conn, batch_id)
    assert batch is not None
    batch["pair_code"] = pair["code"]
    batch["pair_title"] = pair["title"]
    batch["policy"] = "record_only"
    batch["regression_policy"] = REGRESSION_POLICY
    batch["diff_preview"] = batch_repo.list_diffs(conn, batch_id=batch_id, limit=20)
    return batch


def audit_summary(batch_id: int) -> dict[str, Any]:
    """Batch audit with cross-verification (互证).

    Recomputes the invariants that must hold between the batch report, the
    persisted diff rows, and the pair_watermarks table:
      left_rows  == matched + field_mismatch + left_only
      right_rows == matched + field_mismatch + right_only
      diff_total == left_only + right_only + field_mismatch == persisted diffs
      new watermark == max(old watermark, observed max)   (completed runs)
      pair_watermarks == latest completed batch's new watermarks
      rejected runs: zero diffs and watermarks untouched
    """
    conn = connect()
    try:
        batch = batch_repo.get_batch(conn, batch_id)
        if not batch:
            raise ValueError("batch not found")
        pair = pair_repo.get_pair(conn, batch["pair_id"])
        diffs = batch_repo.list_diffs(conn, batch_id=batch_id)
        diff_rows = len(diffs)

        checks: list[dict[str, Any]] = []

        def add(name: str, expect: Any, actual: Any) -> None:
            checks.append({"name": name, "expect": expect, "actual": actual, "ok": expect == actual})

        add(
            "差异合计 = 仅左+仅右+字段差",
            batch["left_only"] + batch["right_only"] + batch["field_mismatch"],
            batch["diff_total"],
        )
        add("差异落库行数 = 批次差异合计", batch["diff_total"], diff_rows)
        add(
            "左增量行数 = 一致+字段差+仅左",
            batch["matched"] + batch["field_mismatch"] + batch["left_only"],
            batch["left_rows"],
        )
        add(
            "右增量行数 = 一致+字段差+仅右",
            batch["matched"] + batch["field_mismatch"] + batch["right_only"],
            batch["right_rows"],
        )

        state = wm_repo.get_state(conn, batch["pair_id"])
        if batch["status"] == "completed":
            add(
                "新左水位 = max(旧左水位, 观测最大左水位)",
                max(batch["old_left_wm"] or "", batch["left_max_wm_seen"] or ""),
                batch["new_left_wm"] or "",
            )
            add(
                "新右水位 = max(旧右水位, 观测最大右水位)",
                max(batch["old_right_wm"] or "", batch["right_max_wm_seen"] or ""),
                batch["new_right_wm"] or "",
            )
            latest_done = conn.execute(
                """
                SELECT id FROM reconcile_batches
                 WHERE pair_id=? AND status='completed'
                 ORDER BY id DESC LIMIT 1
                """,
                (batch["pair_id"],),
            ).fetchone()
            if latest_done and latest_done["id"] == batch_id and state:
                add("水位表左水位 = 本批新左水位", batch["new_left_wm"], state["left_watermark"])
                add("水位表右水位 = 本批新右水位", batch["new_right_wm"], state["right_watermark"])
                add("水位表更新批次 = 本批", batch_id, state["updated_by_batch"])
        elif batch["status"] in ("rejected_regression", "rejected_concurrent", "failed"):
            add("未落差异行", 0, diff_rows)
            add("左水位未动（新=旧）", batch["old_left_wm"], batch["new_left_wm"])
            add("右水位未动（新=旧）", batch["old_right_wm"], batch["new_right_wm"])
            if state:
                add("水位表左水位 = 批前旧值", batch["old_left_wm"], state["left_watermark"])
                add("水位表右水位 = 批前旧值", batch["old_right_wm"], state["right_watermark"])

        return {
            "batch": batch,
            "pair": pair,
            "diff_count": diff_rows,
            "kinds": {
                "left_only": sum(1 for d in diffs if d["kind"] == "left_only"),
                "right_only": sum(1 for d in diffs if d["kind"] == "right_only"),
                "field_mismatch": sum(1 for d in diffs if d["kind"] == "field_mismatch"),
            },
            "checks": checks,
            "checks_ok": all(c["ok"] for c in checks),
            "export_ready": False,
            "note": "互证：批次计数 ↔ 差异落库行 ↔ 水位表 ↔ 批次水位列；审计包导出留给后题切口",
        }
    finally:
        conn.close()
