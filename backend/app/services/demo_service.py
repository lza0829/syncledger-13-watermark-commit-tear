"""Demo-only fixture drivers: advance sources, roll them back, reset the demo.

These endpoints exist so the UI can demonstrate the watermark mechanics
(forward success / regression policy / concurrency guard) against local
fixtures. They mutate fixture JSON files, never the reconcile engine's rules.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import BASELINE_FIXTURES_DIR, FIXTURES_DIR
from app.db import connect
from app.repositories import pairs as pair_repo
from app.repositories import watermarks as wm_repo


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load(name: str) -> list[dict[str, Any]]:
    with (FIXTURES_DIR / name).open(encoding="utf-8") as f:
        return json.load(f)


def _save(name: str, rows: list[dict[str, Any]]) -> None:
    with (FIXTURES_DIR / name).open("w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
        f.write("\n")


def _max_wm(rows: list[dict[str, Any]], field: str) -> str:
    values = [str(r.get(field) or "") for r in rows if str(r.get(field) or "")]
    return max(values) if values else ""


def _get_pair_or_raise(pair_code: str) -> dict[str, Any]:
    conn = connect()
    try:
        pair = pair_repo.get_pair_by_code(conn, pair_code)
        if not pair:
            raise ValueError("pair not found")
        return pair
    finally:
        conn.close()


def advance_fixtures(pair_code: str, rows: int = 1, mismatch: bool = False) -> dict[str, Any]:
    """Append new rows (watermark forward) to BOTH sides of the pair.

    Compare-field values are copied from the left template to both sides, so
    the appended rows align cleanly unless mismatch=True, in which case the
    first appended row gets a tweaked right-side compare field.
    """
    pair = _get_pair_or_raise(pair_code)
    left = _load(pair["left_fixture"])
    right = _load(pair["right_fixture"])
    if not left or not right:
        raise ValueError("fixtures are empty; reset the demo first")

    left_field, right_field = pair["left_wm_field"], pair["right_wm_field"]
    stamp = datetime.now(timezone.utc)
    base = max(
        _max_wm(left, left_field),
        _max_wm(right, right_field),
        stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    base_dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    key_tag = stamp.strftime("%H%M%S")
    left_template, right_template = left[-1], right[-1]
    for i in range(rows):
        wm_value = (base_dt + timedelta(seconds=1 + i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_left = dict(left_template)
        new_right = dict(right_template)
        # Fresh align key: regenerate the first key field, keep the rest from
        # the template (e.g. store_id stays a real store).
        first_key = pair["align_keys"][0]
        key_value = f"{left_template.get(first_key, 'ROW')}-D{key_tag}-{i + 1:02d}"
        new_left[first_key] = key_value
        new_right[first_key] = key_value
        for field in pair["compare_fields"]:
            new_right[field] = new_left.get(field)
        if mismatch and i == 0:
            field = pair["compare_fields"][0]
            current = new_left.get(field)
            new_right[field] = current + 1 if isinstance(current, (int, float)) else f"{current}~"
        new_left[left_field] = wm_value
        new_right[right_field] = wm_value
        left.append(new_left)
        right.append(new_right)

    _save(pair["left_fixture"], left)
    _save(pair["right_fixture"], right)
    return {
        "pair_code": pair_code,
        "appended": rows,
        "mismatch": mismatch,
        "left_rows": len(left),
        "right_rows": len(right),
        "left_max_wm": _max_wm(left, left_field),
        "right_max_wm": _max_wm(right, right_field),
    }


def _restore_fixture(name: str) -> None:
    shutil.copyfile(BASELINE_FIXTURES_DIR / name, FIXTURES_DIR / name)


def regress_fixtures(pair_code: str) -> dict[str, Any]:
    """Roll the pair's sources back to the baseline snapshot.

    After a successful run the recorded watermarks sit ahead of the baseline
    maxima, so the next run observes a regression and applies the fixed
    policy (reject the whole run, no diffs, watermarks untouched).
    """
    pair = _get_pair_or_raise(pair_code)
    _restore_fixture(pair["left_fixture"])
    _restore_fixture(pair["right_fixture"])
    left = _load(pair["left_fixture"])
    right = _load(pair["right_fixture"])
    return {
        "pair_code": pair_code,
        "restored": [pair["left_fixture"], pair["right_fixture"]],
        "left_max_wm": _max_wm(left, pair["left_wm_field"]),
        "right_max_wm": _max_wm(right, pair["right_wm_field"]),
    }


def reset_demo() -> dict[str, Any]:
    """Restore all fixtures from baseline and wipe batches/diffs/watermarks."""
    conn = connect()
    try:
        fixtures = [
            row["f"]
            for row in conn.execute(
                "SELECT left_fixture f FROM reconcile_pairs UNION SELECT right_fixture f FROM reconcile_pairs"
            ).fetchall()
        ]
        for name in fixtures:
            _restore_fixture(name)
        conn.execute("DELETE FROM reconcile_diffs")
        conn.execute("DELETE FROM reconcile_batches")
        conn.execute("DELETE FROM pair_watermarks")
        now = _now()
        for row in conn.execute("SELECT id FROM reconcile_pairs").fetchall():
            wm_repo.ensure_state(conn, row["id"], now)
        conn.commit()
        return {"restored_fixtures": sorted(fixtures), "watermarks_cleared": True}
    finally:
        conn.close()
