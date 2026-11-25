# 银行到账对账

本模块只把**已签发应付**纳入银行到账匹配；未签发、草稿或作废应付不会产生销账。

## 规则实现

1. 一笔银行到账可按明确的 `payable_ids` / `allocations` 或金额子集匹配多笔应付。
2. 唯一金额组合：当天生成销账记录，应付变为 `SETTLED` 或 `PARTIAL`。
3. 多个金额组合都对得上：
   - 所有候选组合中共同包含、归属可确认的份额立即结清；
   - 归属不唯一的剩余份额写入挂账（`ATTRIBUTION_AMBIGUOUS`），不会猜测销账。
4. 晚到流水只根据当前 `remaining_amount` 找剩余份额；已有 `allocation` 不可删除、转移或重新分配。
5. 银行流水号是幂等键。重复提交或调用重试接口只返回首次结果并增加 `replay_count`，不会新增销账/挂账。
6. 工作台和 `GET /api/bank/state` 展示每个应付状态、已结金额、剩余金额、挂账金额与候选归属组合。

金额建议使用 `amount_cents` 整数分；普通 `amount` 支持整数或精确十进制定点数。

## 主要接口

- `GET /bank`：银行对账工作台页面（首页下方也有同一面板）。
- `GET /api/bank/state` / `GET /api/reconcile`：当前台账、销账、挂账和汇总。
- `POST /api/bank/payables` / `POST /api/payables`：登记应付。
- `POST /api/bank/transactions` / `POST /api/bank-transactions`：处理一条或多条银行到账。
- `POST /api/bank/reconcile` / `POST /api/reconcile`：一次提交 `{payables, bank_transactions}`。
- `POST /api/bank/transactions/<流水号>/retry`：幂等重试。
- `POST /api/bank/suspense/<挂账号>/claim`：把挂账份额人工认领给剩余应付。
- `POST /api/bank/demo`：载入内置场景，`{"through":"first|late|all"}`。
- `POST /api/bank/reset`：重置银行对账状态。

无状态核心入口：

```python
from app.reconcile.bank import reconcile_bank

report = reconcile_bank(
    payables=[{"payable_id": "P1", "amount_cents": 10000, "status": "ISSUED"}],
    bank_transactions=[{"bank_transaction_id": "B1", "amount_cents": 10000}],
)
```

有状态顺序处理可使用 `BankReconciler`；HTTP 层使用 SQLite 中的单行状态和 `BEGIN IMMEDIATE` 写事务。
