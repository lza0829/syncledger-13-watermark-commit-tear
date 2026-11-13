"""Incremental extraction over per-endpoint watermarks.

Each reconcile pair tracks one extraction watermark per side (left/right).
A run extracts only rows whose watermark field is strictly greater than the
recorded watermark, then aligns just that increment.

Regression policy — fixed, not a switch
----------------------------------------
When the source's current maximum watermark falls BELOW the watermark recorded
by the last successful batch (source rolled back / reloaded older data), the
chosen default strategy is ``reject_run``:

    整趟拒绝、不落差异、水位不动
    (reject the whole run: persist no diffs, move neither watermark)

The alternative (discard both watermarks, run a full alignment and re-record)
was considered and rejected for this cut. Both behaviors must not coexist as
a verbal toggle, so ``REGRESSION_POLICY`` is a constant with no runtime
override; changing it means changing this file and its tests.
"""

from __future__ import annotations

from typing import Any

REGRESSION_POLICY = "reject_run"

# Watermark values are ISO-8601 UTC strings (…Z); lexicographic compare is
# chronological compare. Empty string = "nothing extracted yet" (first run is
# a full baseline by construction).
EMPTY_WATERMARK = ""


def max_watermark(rows: list[dict[str, Any]], field: str) -> str:
    """Current maximum watermark value observable in the source snapshot."""
    values = [str(r.get(field) or "") for r in rows]
    values = [v for v in values if v]
    return max(values) if values else EMPTY_WATERMARK


def extract_increment(
    rows: list[dict[str, Any]], field: str, since: str
) -> list[dict[str, Any]]:
    """Rows with watermark strictly greater than ``since`` (the increment)."""
    return [r for r in rows if str(r.get(field) or "") > since]
