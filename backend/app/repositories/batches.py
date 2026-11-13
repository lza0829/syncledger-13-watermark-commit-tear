from __future__ import annotations

import json
from typing import Any

import sqlite3

ACTIVE_STATUSES = ("extracting", "aligning")


def _batch(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    return dict(r)


def _diff(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    d["align_key"] = json.loads(d.pop("align_key_json"))
    d["left"] = json.loads(d.pop("left_json")) if d.get("left_json") else None
    d["right"] = json.loads(d.pop("right_json")) if d.get("right_json") else None
    d["mismatched_fields"] = json.loads(d.pop("mismatched_fields_json"))
    return d


def list_batches(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM reconcile_batches ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_batch(r) for r in rows]  # type: ignore[misc]


def get_batch(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any] | None:
    return _batch(conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (batch_id,)).fetchone())


def get_active_batch(conn: sqlite3.Connection, pair_id: int) -> dict[str, Any] | None:
    return _batch(
        conn.execute(
            """
            SELECT * FROM reconcile_batches
             WHERE pair_id=? AND status IN ('extracting','aligning')
             ORDER BY id DESC LIMIT 1
            """,
            (pair_id,),
        ).fetchone()
    )


def count_diffs(conn: sqlite3.Connection, batch_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM reconcile_diffs WHERE batch_id=?", (batch_id,)
    ).fetchone()["c"]


def list_diffs(
    conn: sqlite3.Connection, *, batch_id: int | None = None, pair_id: int | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM reconcile_diffs WHERE 1=1"
    args: list[Any] = []
    if batch_id is not None:
        sql += " AND batch_id=?"
        args.append(batch_id)
    if pair_id is not None:
        sql += " AND pair_id=?"
        args.append(pair_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [_diff(r) for r in conn.execute(sql, args).fetchall()]  # type: ignore[misc]


def next_batch_no(conn: sqlite3.Connection, now: str) -> str:
    seq = conn.execute("SELECT COALESCE(MAX(id),0)+1 s FROM reconcile_batches").fetchone()["s"]
    return f"RB-{now[:10].replace('-', '')}-{seq:04d}"


def insert_batch(
    conn: sqlite3.Connection,
    *,
    batch_no: str,
    pair_id: int,
    status: str,
    mode: str,
    reason: str | None,
    old_left_wm: str,
    old_right_wm: str,
    new_left_wm: str,
    new_right_wm: str,
    note: str | None,
    created_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO reconcile_batches(
            batch_no, pair_id, status, mode, reason,
            old_left_wm, old_right_wm, new_left_wm, new_right_wm,
            left_rows, right_rows, matched, left_only, right_only,
            field_mismatch, diff_total, note, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            batch_no,
            pair_id,
            status,
            mode,
            reason,
            old_left_wm,
            old_right_wm,
            new_left_wm,
            new_right_wm,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            note,
            created_at,
        ),
    )
    return int(cur.lastrowid)


def update_batch(conn: sqlite3.Connection, batch_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(
        f"UPDATE reconcile_batches SET {cols} WHERE id=?",
        (*fields.values(), batch_id),
    )


def insert_diff(
    conn: sqlite3.Connection,
    *,
    batch_id: int,
    pair_id: int,
    diff: dict[str, Any],
    created_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO reconcile_diffs(
            batch_id, pair_id, kind, align_key_json, left_json, right_json,
            mismatched_fields_json, severity, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            batch_id,
            pair_id,
            diff["kind"],
            json.dumps(diff["align_key"], ensure_ascii=False),
            json.dumps(diff["left"], ensure_ascii=False) if diff.get("left") is not None else None,
            json.dumps(diff["right"], ensure_ascii=False) if diff.get("right") is not None else None,
            json.dumps(diff.get("mismatched_fields") or [], ensure_ascii=False),
            "medium",
            created_at,
        ),
    )
    return int(cur.lastrowid)
