"""Shared pytest fixtures: a fresh temp DB + Flask test client per test."""
import importlib
from pathlib import Path

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    # Re-import modules so config.DB_PATH picks up the temp path.
    import config
    importlib.reload(config)
    import database
    importlib.reload(database)
    import subscriptions
    importlib.reload(subscriptions)
    import app as app_module
    importlib.reload(app_module)
    app_module.init_db()
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as c:
        yield c


def make_sub(client, **over):
    body = {
        "name": "Netflix", "category_id": 1,
        "billing_cycle": "monthly", "amount": 12.99,
        "start_date": "2023-01-01", "renew_date": "2030-01-01",
    }
    body.update(over)
    return client.post("/api/subscriptions", json=body)
