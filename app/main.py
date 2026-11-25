import os

from flask import Flask, jsonify, render_template
from werkzeug.exceptions import RequestEntityTooLarge

from app.catalog import SPEC
from app.config import MAX_FILE, ROOT
from app.errors import DomainIssue, error_response
from app.reconcile.bank import BankReconciliationError
from app.reconcile.service import BankReconciliationService
from app.reconcile.store import BankReconciliationStore
from app.reconcile.web import bank_api
from app.review.store import Store
from app.routes import api


def create_app(database=None):
    app = Flask(__name__)
    app.url_map.strict_slashes = False
    database_path = database or os.getenv("WORKBENCH_DB", str(ROOT / "instance" / "workbench.sqlite3"))
    app.config["MAX_CONTENT_LENGTH"] = MAX_FILE + 64 * 1024
    app.extensions["runs"] = Store(database_path)
    bank_store = BankReconciliationStore(database_path)
    app.extensions["bank_store"] = bank_store
    bank_service = BankReconciliationService(bank_store)
    app.extensions["bank"] = bank_service
    app.extensions["bank_reconciliation"] = bank_service
    app.extensions["reconciliation"] = bank_service
    app.register_error_handler(DomainIssue, error_response)
    def bank_error_response(exc):
        return jsonify(error=str(exc), code=exc.code, detail=exc.detail), exc.status

    app.register_error_handler(BankReconciliationError, bank_error_response)
    app.register_error_handler(RequestEntityTooLarge, lambda _: error_response(DomainIssue("FILE_TOO_LARGE", 413)))
    app.register_blueprint(api)
    app.register_blueprint(bank_api)

    @app.get("/")
    def home():
        return render_template("index.html", p=SPEC)

    @app.get("/healthz")
    def health():
        return jsonify(status="ok")

    return app


app = create_app()
