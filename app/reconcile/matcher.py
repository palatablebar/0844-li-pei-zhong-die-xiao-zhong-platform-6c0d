from app.reconcile.bank import *  # noqa: F401,F403
from app.reconcile.bank import (  # noqa: F401
    BankMatcher,
    BankReconciler,
    match_bank_transactions,
    match_payables,
    reconcile_bank,
    reconcile_bank_records,
)


def __getattr__(name):
    from app.reconcile import bank

    try:
        return getattr(bank, name)
    except AttributeError:
        return bank.__getattr__(name)
