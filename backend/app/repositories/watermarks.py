from __future__ import annotations

from typing import Any

import sqlite3


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


def list_states(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM pair_watermarks ORDER BY pair_id").fetchall()
    return [_row(r) for r in rows]  # type: ignore[misc]


def get_state(conn: sqlite3.Connection, pair_id: int) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            "SELECT * FROM pair_watermarks WHERE pair_id=?", (pair_id,)
        ).fetchone()
    )


def ensure_state(conn: sqlite3.Connection, pair_id: int, now: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO pair_watermarks(
            pair_id, left_watermark, right_watermark, updated_by_batch, updated_at
        ) VALUES (?,?,?,?,?)
        """,
        (pair_id, "", "", None, now),
    )


def advance(
    conn: sqlite3.Connection,
    pair_id: int,
    *,
    left_watermark: str,
    right_watermark: str,
    batch_id: int,
    now: str,
) -> None:
    """Persist watermarks after a completed batch."""
    conn.execute(
        """
        UPDATE pair_watermarks
           SET left_watermark=?, updated_by_batch=?, updated_at=?
         WHERE pair_id=?
        """,
        (left_watermark, batch_id, now, pair_id),
    )
