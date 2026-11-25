from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from app.reconcile.bank import BankReconciler


class BankReconciliationStore:
    """SQLite 中保存一份银行对账工作台状态。

    对账规则需要按流水到达顺序保留已结份额，因此采用单行 JSON 快照；写入时使用
    ``BEGIN IMMEDIATE`` 串行化，避免两个请求同时基于旧状态销账。
    """

    def __init__(self, path: str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=8)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self) -> None:
        with self.connect() as db:
            db.execute("""
            CREATE TABLE IF NOT EXISTS bank_reconciliation_state(
              id INTEGER PRIMARY KEY CHECK (id = 1),
              payload TEXT NOT NULL,
              updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """)

    def load_state(self) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM bank_reconciliation_state WHERE id=1"
            ).fetchone()
        if row is None:
            return {}
        return json.loads(row["payload"])

    def save_state(self, state: dict[str, Any]) -> None:
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT INTO bank_reconciliation_state(id, payload, updated_at)
                VALUES(1, ?, datetime('now'))
                ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,
                                              updated_at=datetime('now')
                """,
                (payload,),
            )

    def snapshot(self, today: str | None = None) -> dict[str, Any]:
        return BankReconciler(state=self.load_state(), today=today).snapshot()

    def update(self, callback: Callable[[BankReconciler], Any], *, today: str | None = None) -> Any:
        """在一个立即写事务中读取、修改并保存状态。"""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM bank_reconciliation_state WHERE id=1"
            ).fetchone()
            state = json.loads(row["payload"]) if row else {}
            reconciler = BankReconciler(state=state, today=today)
            result = callback(reconciler)
            new_state = json.dumps(reconciler.export_state(), ensure_ascii=False, sort_keys=True)
            db.execute(
                """
                INSERT INTO bank_reconciliation_state(id, payload, updated_at)
                VALUES(1, ?, datetime('now'))
                ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,
                                              updated_at=datetime('now')
                """,
                (new_state,),
            )
            return result

    def reset(self) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM bank_reconciliation_state WHERE id=1")


# 常见命名别名。
BankStore = BankReconciliationStore
ReconciliationStore = BankReconciliationStore
Store = BankReconciliationStore
