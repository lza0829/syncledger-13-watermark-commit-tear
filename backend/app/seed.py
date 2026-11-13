from __future__ import annotations

import json
from datetime import datetime, timezone

from app.db import connect


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


PAIR_SEEDS = [
    {
        "code": "inventory_hq_vs_store",
        "title": "总部库存 ↔ 门店库存",
        "domain": "inventory",
        "left_name": "总部库存夹具",
        "right_name": "门店库存夹具",
        "left_fixture": "inventory_hq.json",
        "right_fixture": "inventory_store.json",
        "align_keys": ["sku_id", "store_id"],
        "compare_fields": ["qty", "unit"],
        "left_wm_field": "updated_at",
        "right_wm_field": "updated_at",
        "enabled": 1,
    },
    {
        "code": "payment_vs_ledger",
        "title": "支付网关 ↔ 账务流水",
        "domain": "payment",
        "left_name": "支付网关夹具",
        "right_name": "账务流水夹具",
        "left_fixture": "payment_gateway.json",
        "right_fixture": "payment_ledger.json",
        "align_keys": ["txn_id"],
        "compare_fields": ["amount", "currency"],
        "left_wm_field": "paid_at",
        "right_wm_field": "booked_at",
        "enabled": 1,
    },
    {
        "code": "sku_catalog_a_vs_b",
        "title": "商品目录 A ↔ 目录 B",
        "domain": "catalog",
        "left_name": "目录 A 夹具",
        "right_name": "目录 B 夹具",
        "left_fixture": "sku_catalog_a.json",
        "right_fixture": "sku_catalog_b.json",
        "align_keys": ["sku_code"],
        "compare_fields": ["name", "category"],
        "left_wm_field": "updated_at",
        "right_wm_field": "updated_at",
        "enabled": 1,
    },
]

CONTRACT_SEEDS = [
    ("inventory_hq_vs_store", "left", ["sku_id", "store_id", "qty", "unit", "updated_at"]),
    ("inventory_hq_vs_store", "right", ["sku_id", "store_id", "qty", "unit", "updated_at"]),
    ("payment_vs_ledger", "left", ["txn_id", "amount", "currency", "paid_at"]),
    ("payment_vs_ledger", "right", ["txn_id", "amount", "currency", "booked_at"]),
    ("sku_catalog_a_vs_b", "left", ["sku_code", "name", "category", "updated_at"]),
    ("sku_catalog_a_vs_b", "right", ["sku_code", "name", "category", "updated_at"]),
]


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reconcile_pairs(
                id INTEGER PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                domain TEXT NOT NULL,
                left_name TEXT NOT NULL,
                right_name TEXT NOT NULL,
                left_fixture TEXT NOT NULL,
                right_fixture TEXT NOT NULL,
                align_keys_json TEXT NOT NULL,
                compare_fields_json TEXT NOT NULL,
                left_wm_field TEXT NOT NULL DEFAULT 'updated_at',
                right_wm_field TEXT NOT NULL DEFAULT 'updated_at',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconcile_batches(
                id INTEGER PRIMARY KEY,
                batch_no TEXT NOT NULL UNIQUE,
                pair_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                mode TEXT,
                reason TEXT,
                old_left_wm TEXT,
                old_right_wm TEXT,
                new_left_wm TEXT,
                new_right_wm TEXT,
                left_max_wm_seen TEXT,
                right_max_wm_seen TEXT,
                left_rows INTEGER NOT NULL,
                right_rows INTEGER NOT NULL,
                matched INTEGER NOT NULL,
                left_only INTEGER NOT NULL,
                right_only INTEGER NOT NULL,
                field_mismatch INTEGER NOT NULL,
                diff_total INTEGER NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL,
                finished_at TEXT,
                FOREIGN KEY(pair_id) REFERENCES reconcile_pairs(id)
            );
            CREATE TABLE IF NOT EXISTS reconcile_diffs(
                id INTEGER PRIMARY KEY,
                batch_id INTEGER NOT NULL,
                pair_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                align_key_json TEXT NOT NULL,
                left_json TEXT,
                right_json TEXT,
                mismatched_fields_json TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'medium',
                created_at TEXT NOT NULL,
                FOREIGN KEY(batch_id) REFERENCES reconcile_batches(id),
                FOREIGN KEY(pair_id) REFERENCES reconcile_pairs(id)
            );
            CREATE TABLE IF NOT EXISTS pair_watermarks(
                pair_id INTEGER PRIMARY KEY,
                left_watermark TEXT NOT NULL DEFAULT '',
                right_watermark TEXT NOT NULL DEFAULT '',
                updated_by_batch INTEGER,
                updated_at TEXT,
                FOREIGN KEY(pair_id) REFERENCES reconcile_pairs(id)
            );
            CREATE TABLE IF NOT EXISTS diff_tickets(
                id INTEGER PRIMARY KEY,
                pair_id INTEGER,
                diff_id INTEGER,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                assignee TEXT,
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(pair_id) REFERENCES reconcile_pairs(id),
                FOREIGN KEY(diff_id) REFERENCES reconcile_diffs(id)
            );
            CREATE TABLE IF NOT EXISTS endpoint_contracts(
                id INTEGER PRIMARY KEY,
                pair_code TEXT NOT NULL,
                side TEXT NOT NULL,
                fields_json TEXT NOT NULL,
                version TEXT NOT NULL DEFAULT 'v1',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        # Migrations for databases created by earlier cuts.
        _ensure_column(conn, "reconcile_pairs", "left_wm_field", "TEXT NOT NULL DEFAULT 'updated_at'")
        _ensure_column(conn, "reconcile_pairs", "right_wm_field", "TEXT NOT NULL DEFAULT 'updated_at'")
        for col, ddl in [
            ("mode", "TEXT"),
            ("reason", "TEXT"),
            ("old_left_wm", "TEXT"),
            ("old_right_wm", "TEXT"),
            ("new_left_wm", "TEXT"),
            ("new_right_wm", "TEXT"),
            ("left_max_wm_seen", "TEXT"),
            ("right_max_wm_seen", "TEXT"),
            ("finished_at", "TEXT"),
        ]:
            _ensure_column(conn, "reconcile_batches", col, ddl)
        conn.execute(
            "UPDATE reconcile_pairs SET left_wm_field='paid_at', right_wm_field='booked_at'"
            " WHERE code='payment_vs_ledger'"
        )
        # At most one in-flight (extracting/aligning) batch per pair — the
        # concurrency guard. New runs that hit this index are rejected.
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_active_batch_per_pair
                ON reconcile_batches(pair_id)
             WHERE status IN ('extracting','aligning')
            """
        )
        now = _now()
        # A crash/restart can strand in-flight batches; close them as failed so
        # the pair is not blocked forever. Watermarks were never touched by
        # such batches (they only move inside the completion transaction).
        conn.execute(
            """
            UPDATE reconcile_batches
               SET status='failed',
                   reason=COALESCE(reason || '；', '') || '服务重启，在途批次按失败关闭（水位未动）',
                   finished_at=?
             WHERE status IN ('extracting','aligning')
            """,
            (now,),
        )
        if conn.execute("SELECT COUNT(*) c FROM reconcile_pairs").fetchone()["c"] == 0:
            for p in PAIR_SEEDS:
                conn.execute(
                    """
                    INSERT INTO reconcile_pairs(
                        code, title, domain, left_name, right_name,
                        left_fixture, right_fixture, align_keys_json, compare_fields_json,
                        left_wm_field, right_wm_field, enabled, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        p["code"],
                        p["title"],
                        p["domain"],
                        p["left_name"],
                        p["right_name"],
                        p["left_fixture"],
                        p["right_fixture"],
                        json.dumps(p["align_keys"], ensure_ascii=False),
                        json.dumps(p["compare_fields"], ensure_ascii=False),
                        p["left_wm_field"],
                        p["right_wm_field"],
                        p["enabled"],
                        now,
                    ),
                )
        # Every pair gets a watermark row (empty = nothing extracted yet).
        for row in conn.execute("SELECT id FROM reconcile_pairs").fetchall():
            conn.execute(
                """
                INSERT OR IGNORE INTO pair_watermarks(
                    pair_id, left_watermark, right_watermark, updated_by_batch, updated_at
                ) VALUES (?,?,?,?,?)
                """,
                (row["id"], "", "", None, now),
            )
        if conn.execute("SELECT COUNT(*) c FROM endpoint_contracts").fetchone()["c"] == 0:
            for pair_code, side, fields in CONTRACT_SEEDS:
                conn.execute(
                    """
                    INSERT INTO endpoint_contracts(pair_code, side, fields_json, version, updated_at)
                    VALUES (?,?,?,?,?)
                    """,
                    (pair_code, side, json.dumps(fields, ensure_ascii=False), "v1", now),
                )
        if conn.execute("SELECT COUNT(*) c FROM settings").fetchone()["c"] == 0:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                ("default_policy", "record_only"),
            )
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                ("auto_overwrite", "false"),
            )
        conn.commit()
    finally:
        conn.close()
