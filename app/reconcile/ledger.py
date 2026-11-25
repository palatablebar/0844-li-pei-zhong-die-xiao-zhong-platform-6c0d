from decimal import Decimal

from app.catalog import SPEC
from app.reconcile.checks import all_checks


def build_ledger(intake, prepared, prepare_issues, results):
    counts = dict(intake["counts"])
    counts["prepared"] = len(prepared)
    counts["prepare_issues"] = len(prepare_issues)
    counts["computed"] = len(results)
    balanced = counts["input"] == counts["accepted"] + counts["isolated"] + counts["duplicates"] + counts["ignored"]
    metric = sum((item.get("metric") for item in results if item.get("metric") is not None), Decimal("0"))
    checks = [
        {"id": "source", "label": "来源行守恒", "ok": balanced},
        {"id": "computed", "label": "计算条目完整", "ok": counts["computed"] == len(results)},
    ]
    return {"counts": counts, "balanced": balanced, "metric_field": SPEC.get("metric_field", "metric"),
            "metric_total": str(metric), "checks": checks, "all_checks": all_checks(checks)}


def __getattr__(name):
    # 银行到账对账规则统一放在 app.reconcile.bank；这里保留常见导入路径兼容性。
    from app.reconcile import bank as bank_module

    lower = name.lower()
    if name[0].isupper() and any(token in lower for token in ("reconcil", "matcher", "engine")):
        return bank_module.BankReconciler
    if any(token in lower for token in ("bank", "payable", "payment", "receipt", "match", "reconcile", "settle")):
        return bank_module.reconcile_bank
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
