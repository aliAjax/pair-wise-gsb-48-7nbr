# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、净额结算、交收完整性和公司行动调整和冲突检查。
- `src/receipts.py`：外部存管交收回执批次校验与对账分类（匹配/引用缺失/版本变化）。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、机构隔离、乐观并发、回执闸门和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景与回执对账测试。

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
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，机构取`X-Org`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

### 外部存管交收回执对账

回执由存管专员（`custody_clerk`）提交、存管主管（`custody_supervisor`）复核，均须带`X-Org`。

- `POST /api/receipt-batches`：回传一批回执，请求体：
  `{"batch_no":"BK-001","items":[{"receipt_no":"R1","reference":"TRD-1","ref_version":2,"result":"settled","delivered_quantity":2000,"cash_paid":25036}]}`。
  - 同一`batch_no`重复回传（含写入失败后按原批次号重试）只记一次，重复请求返回HTTP 200与已存批次（`"created":false,"deduplicated":true`），已完成的复核结果原样保留。
  - 明细按引用与版本自动分类：`matched`（引用存在且版本一致）、`ref_missing`（引用缺失/乱序早到/跨机构引用）、`version_changed`（回执引用版本与当前版本不一致）。批次含待复核项时状态为`pending_review`。
- `POST /api/receipt-items/{id}/decision`：主管对单笔回执下结论 `{"decision":"confirmed|rejected","note":"..."}`；待复核项会拦住指令的审批与交收（返回`409 receipt_pending_review`），确认或驳回后才放行。
- `POST /api/receipt-batches/{id}/recheck`：重新对账整批，迟到指令到达后`ref_missing`自动转匹配。
- `GET /api/receipt-batches`：批次列表（按机构隔离，admin可看全部）。
- `GET /api/receipt-batches/{id}`：批次详情，含每笔回执的`ref_version`、`current_version`、`version_match`。
- `GET /api/receipt-batches/{id}/audit`：批次审计时间线（接收、重复回传、重新对账、复核）。
- `GET /api/records`与`GET /api/records/{id}`：每条指令附带`receipts`（每笔回执与当前版本的对应关系）和`receipt_pending`标记；指令自身审计时间线含`receipt_linked`、`receipt_reviewed`事件。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。专员越权查看/处理其他机构批次返回`403 permission_denied`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
