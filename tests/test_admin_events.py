import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def restore_tool_config():
    yield
    from agent.tools import configure_tools

    configure_tools({})


def _admin_client():
    import main

    client = TestClient(main.app)
    response = client.post(
        "/admin/auth/login",
        json={"password": main.ADMIN_PASSWORD},
    )
    assert response.status_code == 200
    client.cookies.update(response.cookies)
    return client


def test_admin_events_endpoint_returns_dashboard_shape():
    client = _admin_client()

    response = client.get("/admin/events?limit=5")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload["events"], list)
    assert "proactive" in payload
    assert "memory" in payload
    assert "bot" in payload
