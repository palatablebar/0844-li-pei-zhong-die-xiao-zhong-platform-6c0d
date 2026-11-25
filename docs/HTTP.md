# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `GET /api/runs/{id}/evidence`：下载已签发证据。
- `GET /api/examples/{profile}`：下载样例。
- `GET /api/bank/state`：银行到账对账工作台状态。
- `POST /api/bank/reconcile`：提交应付与银行流水并对账（也兼容 `/api/reconcile`）。
- `POST /api/bank/payables`：登记已签发应付。
- `POST /api/bank/transactions`：处理银行到账；相同流水号幂等。
- `POST /api/bank/transactions/{id}/retry`：重试同一到账流水，不重复销账。
- `POST /api/bank/suspense/{id}/claim`：认领挂账份额。
- `POST /api/bank/demo`：载入银行对账演示场景。
- `GET /bank`：银行到账对账工作台。
- `GET /healthz`：健康检查。
