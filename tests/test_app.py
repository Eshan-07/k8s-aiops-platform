import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    main.chaos.latency_ms = 0
    main.chaos.error_percent = 0
    main.chaos.ready = True
    monkeypatch.setattr(main, "CHAOS_ENABLED", False)


def test_health_and_ready():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").status_code == 200


def test_create_and_get_order():
    created = client.post("/orders", json={"item": "book", "quantity": 2})
    assert created.status_code == 201
    order_id = created.json()["id"]
    fetched = client.get(f"/orders/{order_id}")
    assert fetched.status_code == 200
    assert fetched.json()["item"] == "book"


def test_validation_rejects_bad_quantity():
    assert client.post("/orders", json={"item": "x", "quantity": 0}).status_code == 422


def test_unknown_order_is_404():
    assert client.get("/orders/999999").status_code == 404


def test_metrics_exposed():
    client.get("/health")
    body = client.get("/metrics").text
    assert "http_requests_total" in body
    assert "http_request_duration_seconds_bucket" in body


def test_chaos_disabled_returns_404():
    assert client.post("/chaos/errors?percent=50").status_code == 404


def test_chaos_errors_inject_failures(monkeypatch):
    monkeypatch.setattr(main, "CHAOS_ENABLED", True)
    assert client.post("/chaos/errors?percent=100").status_code == 200
    assert client.get("/orders").status_code == 500
    client.post("/chaos/reset")
    assert client.get("/orders").status_code == 200


def test_chaos_unready_flips_readiness(monkeypatch):
    monkeypatch.setattr(main, "CHAOS_ENABLED", True)
    client.post("/chaos/unready?seconds=1")
    assert client.get("/ready").status_code == 503
    client.post("/chaos/reset")
    assert client.get("/ready").status_code == 200