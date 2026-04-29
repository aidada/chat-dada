from __future__ import annotations

from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.brain.defaults import MODEL_CONFIGS as DEFAULT_MODEL_CONFIGS
from agent.brain.registry import registry
from web.deps import get_admin_user
from web.routers.system import router


DEFAULT_MODEL_SNAPSHOT = deepcopy(DEFAULT_MODEL_CONFIGS)


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_admin_user] = lambda: {"id": "test", "email": "admin@test.com"}
    return TestClient(app)


@pytest.fixture(autouse=True)
def _restore_model_configs():
    registry.reset()
    yield
    registry.reset()


def test_get_models_returns_snapshot():
    client = _make_client()

    response = client.get("/api/admin/models")
    assert response.status_code == 200

    data = response.json()
    orchestrator = data["orchestrator"]
    doc_analyst = data["doc_analyst"]
    assert orchestrator["model"] == DEFAULT_MODEL_SNAPSHOT["orchestrator"]["model"]
    assert orchestrator["provider"] == DEFAULT_MODEL_SNAPSHOT["orchestrator"]["provider"]
    assert orchestrator["client_type"] == registry.get("orchestrator").client_type
    assert doc_analyst["model"] == DEFAULT_MODEL_SNAPSHOT["doc_analyst"]["model"]
    assert doc_analyst["provider"] == DEFAULT_MODEL_SNAPSHOT["doc_analyst"]["provider"]


def test_put_model_updates_role():
    client = _make_client()

    response = client.put("/api/admin/models/orchestrator", json={"model": "gpt-6"})
    assert response.status_code == 200
    data = response.json()
    assert data["role"] == "orchestrator"
    assert data["model"] == "gpt-6"
    assert data["provider"] == DEFAULT_MODEL_SNAPSHOT["orchestrator"]["provider"]
    assert data["client_type"] == registry.get("orchestrator").client_type
    assert registry.get("orchestrator").model == "gpt-6"


def test_put_model_can_update_provider():
    client = _make_client()

    response = client.put("/api/admin/models/doc_analyst", json={"provider": "openai"})
    assert response.status_code == 200
    data = response.json()
    assert data["role"] == "doc_analyst"
    assert data["provider"] == "openai"
    assert data["client_type"] == registry.get("doc_analyst").client_type
    assert registry.get("doc_analyst").provider == "openai"


def test_put_model_can_update_to_deepseek_provider():
    client = _make_client()

    response = client.put(
        "/api/admin/models/doc_analyst",
        json={"model": "deepseek-v4-pro", "provider": "deepseek"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["role"] == "doc_analyst"
    assert data["model"] == "deepseek-v4-pro"
    assert data["provider"] == "deepseek"
    assert data["client_type"] == "deepseek_openai"
    assert registry.get("doc_analyst").provider == "deepseek"


def test_put_unknown_role_returns_404():
    client = _make_client()

    response = client.put("/api/admin/models/nonexistent", json={"model": "gpt-6"})
    assert response.status_code == 404


def test_reset_models_restores_defaults():
    client = _make_client()

    client.put("/api/admin/models/orchestrator", json={"model": "gpt-6"})
    response = client.post("/api/admin/models/reset")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "message": "All models reset to defaults"}

    snapshot = client.get("/api/admin/models").json()
    assert snapshot["orchestrator"]["model"] == DEFAULT_MODEL_SNAPSHOT["orchestrator"]["model"]
    assert snapshot["doc_analyst"]["provider"] == DEFAULT_MODEL_SNAPSHOT["doc_analyst"]["provider"]
