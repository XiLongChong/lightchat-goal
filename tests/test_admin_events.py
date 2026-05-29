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
    assert "proactive" not in payload
    assert "memory" in payload
    assert "bot" in payload


def test_stale_or_unbound_contact_is_not_send_ready():
    import main

    assert main._contact_send_block_reason(
        {
            "user_id": "wxid_me",
            "context_token": "ctx",
            "bot_id": "other_bot",
            "updated_at": main.PROCESS_STARTED_AT + 1,
        },
        "default_bot",
    ) == "bot not bound"

    assert main._contact_send_block_reason(
        {
            "user_id": "wxid_me",
            "context_token": "ctx",
            "bot_id": "default_bot",
            "updated_at": main.PROCESS_STARTED_AT - 1,
        },
        "default_bot",
    ) == "stale contact binding"
