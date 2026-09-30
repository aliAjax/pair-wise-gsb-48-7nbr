# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、净额结算、交收完整性、公司行动调整、冲突检查，以及回执校验/指纹/对账分类/结算闸门（`ReceiptRules`）。
- `src/repository.py`：SQLite建表、事务和查询（含回执批次、回执、回执事件、对账尝试四张表）。
- `src/service.py`：用例编排、权限检查、机构隔离、乐观并发、批次互斥、写入重试和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景以及回执对账测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8324
```

默认端口为`8324`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

## 外部存管回执对账

外部存管回传交收回执常**重复、乱序**，或在本地结算指令修改后才到。回执对账已接入结算流程。

### 角色（`X-Role`）

| 角色 | 权限 |
| --- | --- |
| `custody_clerk`（存管专员） | 登记回执批次、查询本机构回执；不能对账/复核 |
| `settlement_officer`（结算主管） | 对账批次、待复核人工处理、审批与交收 |
| `admin` | 跨机构只读/全部权限 |

回执业务必须带`X-Org`机构标识；指令创建时也会记录`X-Org`。专员/主管只能处理本机构批次，越机构返回`403`。

### 接口

- `POST /api/receipt-batches`：专员登记批次，`{"batch_no":"B-001","receipts":[...]}`。
  回执条目：`seq`（批次内序号，重复报错）、`reference`（本地指令引用）、`local_version`（回执对应的本地版本）、`delivered_quantity`、`cash_paid`，可选`payload_hash`、`external_id`、`note`。
- `POST /api/receipt-batches/{batchNo}/reconcile`：主管对账。
- `GET /api/receipt-batches`：批次列表（机构隔离）。
- `GET /api/receipt-batches/{batchNo}`：批次详情与汇总。
- `GET /api/receipt-batches/{batchNo}/audit`：批次内每笔回执事件 + 历次处理尝试。
- `GET /api/receipts?batch_no=&reference=&status=`：回执列表，每条带`correspondence`（回执版本↔指令当前版本/哈希是否对应）。
- `GET /api/receipts/{id}`：单笔回执与对应关系。
- `POST /api/receipts/{id}/review`：主管对待复核回执处理，`{"decision":"match|reject","note":"..."}`。

### 对账语义

1. **重复只记一次**：同一批次号重发（哪怕条目乱序）整体幂等；跨批次重发的同一回执按业务指纹（机构+引用+external_id+版本+数量+金额）去重。
2. **自动分类**：
   - 引用缺失 → `pending_review/missing_reference`；引用的指令后来出现后，**同一主管按原批次号重放**只重扫这类回执。
   - 回执版本 ≠ 指令当前版本，或`payload_hash`与当前指令哈希不一致 → `pending_review/version_changed|payload_changed`（本地改过）。
   - 跨机构引用、交收数量不符、资金不足 → 相应原因的待复核。
   - 全部一致 → `matched`，记录匹配时的版本与哈希快照。
3. **处理前审批和交收停住**：指令存在待复核回执时，`approve`/`settle` 返回`409`；批次尚未对账（只有已登记回执）时交收也被拦下。待复核须主管在`review`接口人工`match`/`reject`后才能继续。
4. **同批次只放行一位主管**：对账在单事务内`BEGIN IMMEDIATE`认领批次；两位主管并发提交时只有一位成功，另一位收到`409 batch_completed`（处理中则`batch_locked`）。同一主管对已完成批次的重放为幂等刷新，不产生重复事件。
5. **写入失败按原批次号重试**：对账遇到存储错误整体回滚后自动重试（默认3次），每次尝试写入`reconciliation_attempts`；成功后已完成结果保留，重放不重复记账。重试耗尽返回`503 retry_exhausted`。
6. **越权拒绝**：专员不能对账/复核；任何角色处理非本机构批次、批次号、回执均被`403`；列表按机构过滤。
7. **可追溯**：`GET /api/receipts`的`correspondence`展示每笔回执版本/哈希与当前指令版本的对应（本地改过后显示`version_matches=false`）；`GET /api/records/{id}`含`receipt_summary`，其`audit`时间线合并了回执事件（匹配/待复核/人工处理/交收印证）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及回执去重、乱序、缺单补单、版本变化、审批交收闸门、主管并发互斥、写入重试和机构越权。
