# 03-syncledger（双核对账）

Syncledger — 跨库/跨系统对齐对账：双端抽取 → 对齐键对账 → 差异处置 → 批次互证。

与 Feedstitch（多源 Feed 归并）、Batchgate（作业依赖编排）主链路不同。夹具全本地，不连活库。

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5300 |
| API | http://localhost:10300 |

## 主链

配置对账对与对齐键 → 触发双端抽取并对齐 → 落差异行与批次计数 → 页面互证

## 增量水位切口（本切口已实现）

- **按对分端水位**：`pair_watermarks` 每对一行，左/右各记一个抽取水位（库存/目录用 `updated_at`，支付用 `paid_at`/`booked_at`）。本趟只抽取 `水位字段 > 已记水位` 的增量并对齐，不再整表重抽、不重出上趟区间差异。
- **两端同事务推进**：差异行 + 批次完成 + 左右水位在同一个 SQLite 事务提交——要么一起推进，要么一起不动；引擎只暴露双端同写的 `advance()`，没有单端更新入口。中途失败批次记 `failed`，水位保持批前值。
- **回退策略（定死一种，非开关）**：源端观测最大水位低于上次成功批次记录值时，固定执行 **整趟拒绝、不落差异、水位不动**（`engines/incremental.py` 的 `REGRESSION_POLICY = "reject_run"` 常量，无运行时开关；「丢弃水位走全量重记」方案已弃选）。拒绝也落批次行，写明原因与旧/新水位。
- **并发互斥**：部分唯一索引 `uq_active_batch_per_pair`（`status IN ('extracting','aligning')`）保证每对最多一个在途批次；撞索引即拒，HTTP 409，并落 `rejected_concurrent` 批次留原因。服务重启会把 stranded 在途批次按 `failed` 关闭，避免永久卡死。
- **互证**：`/api/audit/batches/{id}` 重算不变量——`左增量行 = 一致+字段差+仅左`（右侧同理）、`差异合计 = 落库差异行数`、`新水位 = max(旧水位, 观测最大)`、水位表值 = 最新完成批的新水位；拒绝批则核对「零差异、水位未动」。
- **演示**：对账台页面可直接操作——①追加增量（可制造字段差）→ 前进成功；②源端回滚基线 → 再跑触发整趟拒绝；③并发开跑 → 一趟完成一趟 409；另有演示重置。对应 API：`POST /api/demo/advance/{pair_code}`、`/api/demo/regress/{pair_code}`、`/api/demo/reset`。

## 本地运行（不用 Docker）

```bash
pip install -r backend/requirements.txt
cd backend && uvicorn app.main:app --port 10300
cd frontend && npm install && npm run dev   # http://127.0.0.1:5300
cd backend && python3 -m pytest tests/      # 8 个用例：前进/回退/并发/原子性/互证
```

## 种子对账对

- **inventory_hq_vs_store**：总部库存 ↔ 门店库存（`sku_id`+`store_id`，比对 `qty`）
- **payment_vs_ledger**：支付网关 ↔ 账务流水（`txn_id`，比对 `amount`/`currency`）
- **sku_catalog_a_vs_b**：目录 A ↔ 目录 B（`sku_code`，比对 `name`/`category`）

底座默认只**记差异、不自动盖写**；差异处置/契约/审计页留表与入口，深逻辑留给后题。侧栏不叫「工单」，避免撞禁名单「工单客服」。

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。

## 0-1 切口（未实现，见出题规划）

`master_slave_pick` / `side_only_policy` / `window_buffer` / `diff_disposition_flow` / `batch_idempotency` / …

（`incremental_watermark` 切口已落地，见上节。）
