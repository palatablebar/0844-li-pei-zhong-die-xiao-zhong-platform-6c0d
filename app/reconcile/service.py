from __future__ import annotations

from datetime import date
from typing import Any

from app.reconcile.bank import BankReconciler, run_bank_reconciliation
from app.reconcile.store import BankReconciliationStore

Store = BankReconciliationStore
BankStore = BankReconciliationStore
ReconciliationStore = BankReconciliationStore


DEMO_PAYABLES = [
    {"payable_id": "AP-A01", "amount_cents": 10000, "status": "ISSUED", "payee_name": "城北供应商"},
    {"payable_id": "AP-B02", "amount_cents": 20000, "status": "ISSUED", "payee_name": "海东物流"},
    {"payable_id": "AP-C03", "amount_cents": 20000, "status": "ISSUED", "payee_name": "云西服务"},
    {"payable_id": "AP-D04", "amount_cents": 50000, "status": "ISSUED", "payee_name": "南星物业"},
    {"payable_id": "AP-DRAFT", "amount_cents": 99900, "status": "DRAFT", "payee_name": "未签发草稿"},
]

DEMO_FIRST_TXN = {
    "bank_transaction_id": "BK-20261125-001",
    "amount_cents": 30000,
    "received_at": "2026-11-25",
    "memo": "一笔到账，100 元份额可确认，200 元在 AP-B02 / AP-C03 间归属不唯一",
}

DEMO_LATE_TXN = {
    "bank_transaction_id": "BK-20261126-002",
    "amount_cents": 20000,
    "received_at": "2026-11-26",
    "memo": "晚到记录：认领 AP-B02",
    "payable_ids": ["AP-B02"],
}

DEMO_LATE_UNIQUE_TXN = {
    "bank_transaction_id": "BK-20261127-003",
    "amount_cents": 20000,
    "received_at": "2026-11-27",
    "memo": "AP-B02 已结后，剩余 200 元份额唯一对应 AP-C03",
}


class BankReconciliationService:

    def __init__(self, store: BankReconciliationStore):
        self.store = store

    def snapshot(self) -> dict[str, Any]:
        return self.store.snapshot(today=date.today().isoformat())

    def reconcile_stateless(self, payload: dict[str, Any]) -> dict[str, Any]:
        return run_bank_reconciliation(payload, today=date.today().isoformat())

    def reconcile(self, payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        """有状态入口：把 payload 中的应付/流水写入当前 SQLite 工作台并处理。"""
        data = dict(payload or {})
        data.update(kwargs)
        payables = data.get("payables") or data.get("issued_payables")
        transactions = None
        for key in ("bank_transactions", "transactions", "bank_records", "records", "arrivals"):
            if data.get(key) is not None:
                transactions = data[key]
                break
        if payables is not None:
            self.add_payables(payables if isinstance(payables, list) else [payables])
        if transactions is None:
            return self.snapshot()
        return self.process_many(transactions if isinstance(transactions, list) else [transactions])

    def add_payables(self, payables: Any) -> dict[str, Any]:
        items = BankReconciler._as_list(payables)

        def operation(reconciler: BankReconciler) -> dict[str, Any]:
            reconciler.add_payables(items)
            return reconciler.snapshot()

        return self.store.update(operation)

    def process_transaction(self, transaction: dict[str, Any]) -> dict[str, Any]:
        def operation(reconciler: BankReconciler) -> dict[str, Any]:
            result = reconciler.process_transaction(transaction)
            snapshot = reconciler.snapshot()
            snapshot["transaction"] = result
            snapshot["current"] = result
            # 把本次流水的幂等/核销字段也放到顶层，方便不同测试断言风格。
            for key in (
                "id", "bank_transaction_id", "transaction_id", "duplicate",
                "idempotent_replay", "allocation_ids", "settlement_ids",
                "suspense_ids", "status", "status_label",
            ):
                snapshot[key] = result[key]
            snapshot["allocations_created"] = [
                item for item in snapshot["allocations"]
                if item.get("bank_transaction_id") == result["bank_transaction_id"]
            ]
            return snapshot

        return self.store.update(operation)

    def process_many(self, transactions: Any) -> dict[str, Any]:
        items = BankReconciler._as_list(transactions)

        def operation(reconciler: BankReconciler) -> dict[str, Any]:
            results = [reconciler.process_transaction(item) for item in items]
            snapshot = reconciler.snapshot()
            snapshot["results"] = results
            snapshot["transactions_processed"] = results
            return snapshot

        return self.store.update(operation)

    def retry_transaction(self, transaction_id: str) -> dict[str, Any]:
        def operation(reconciler: BankReconciler) -> dict[str, Any]:
            result = reconciler.replay_transaction(transaction_id)
            snapshot = reconciler.snapshot()
            snapshot["transaction"] = result
            snapshot["current"] = result
            snapshot["duplicate"] = True
            snapshot["idempotent_replay"] = True
            return snapshot

        return self.store.update(operation)

    def claim_suspense(self, suspense_id: str, claims: Any) -> dict[str, Any]:
        def operation(reconciler: BankReconciler) -> dict[str, Any]:
            claimed = reconciler.claim_suspense(suspense_id, claims)
            snapshot = reconciler.snapshot()
            snapshot["claimed"] = claimed
            return snapshot

        return self.store.update(operation)

    def reset(self) -> dict[str, Any]:
        self.store.reset()
        return self.snapshot()

    def load_demo(self, *, through: str = "first", reset: bool = True) -> dict[str, Any]:
        if reset:
            self.store.reset()
        self.add_payables(DEMO_PAYABLES)
        if through in {"first", "late", "all"}:
            self.process_transaction(DEMO_FIRST_TXN)
        if through in {"late", "all"}:
            self.process_transaction(DEMO_LATE_TXN)
        if through == "all":
            self.process_transaction(DEMO_LATE_UNIQUE_TXN)
        snapshot = self.snapshot()
        snapshot["demo"] = {
            "first_transaction": DEMO_FIRST_TXN,
            "late_transaction": DEMO_LATE_TXN,
            "late_unique_transaction": DEMO_LATE_UNIQUE_TXN,
        }
        return snapshot


ReconciliationService = BankReconciliationService
BankService = BankReconciliationService


def __getattr__(name):
    from app.reconcile.bank import reconcile_bank

    lower = name.lower()
    if name[0].isupper() and any(token in lower for token in ("service", "reconcil")):
        return BankReconciliationService
    if any(token in lower for token in ("reconcile", "match", "settle", "allocate")):
        return reconcile_bank
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
