from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import access_guard, app, warehouse


@pytest.fixture(autouse=True)
def reset_security_state(monkeypatch):
    monkeypatch.setattr(config, "PORTAL_TRIGGER_PASSCODE", "")
    monkeypatch.setattr(config, "PORTAL_USER_TOKENS", "")
    monkeypatch.setattr(config, "PORTAL_VIEW_PASSCODE", "")
    monkeypatch.setattr(config, "ALLOWED_TRIGGER_PLAYERS", "")
    monkeypatch.setattr(config, "TRIGGER_COOLDOWN_SECONDS", 120)
    monkeypatch.setattr(config, "TRIGGER_MAX_PER_HOUR", 10)
    access_guard._player_last_trigger.clear()
    access_guard._ip_trigger_timestamps.clear()
    yield


client = TestClient(app)


def test_health_and_auth_status_open():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["trigger_protected"] is False

    auth = client.get("/api/auth/status")
    assert auth.status_code == 200
    assert auth.json()["trigger_authorized"] is True
    assert auth.json()["view_authorized"] is True


def test_trigger_requires_token_when_protected(monkeypatch):
    monkeypatch.setattr(config, "PORTAL_USER_TOKENS", "kingsley:key-123,guest:key-456")

    # Missing token -> 401
    resp = client.post("/api/pipeline/trigger", json={"username": "Real_Vick"})
    assert resp.status_code == 401

    # Wrong token -> 401
    resp_bad = client.post(
        "/api/pipeline/trigger",
        json={"username": "Real_Vick"},
        headers={"X-Portal-Auth": "wrong-key"},
    )
    assert resp_bad.status_code == 401

    # Valid subset user token -> succeeds (mocking GitHub/Lichess calls)
    with patch(
        "app.main.github_client.fetch_lichess_user_profile",
        return_value={"username": "Real_Vick"},
    ), patch(
        "app.main.github_client.trigger_workflow",
        return_value={"status": "dispatched", "username": "Real_Vick", "run": None},
    ):
        resp_ok = client.post(
            "/api/pipeline/trigger",
            json={"username": "Real_Vick"},
            headers={"X-Portal-Auth": "key-123"},
        )
        assert resp_ok.status_code == 200
        assert resp_ok.json()["triggered_by"] == "kingsley"


def test_allowed_players_subset_and_cooldown(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_TRIGGER_PLAYERS", "Wizard-King,Real_Vick")
    monkeypatch.setattr(config, "TRIGGER_COOLDOWN_SECONDS", 60)

    # Disallowed player -> 403
    resp_forbidden = client.post(
        "/api/pipeline/trigger", json={"username": "MagnusCarlsen"}
    )
    assert resp_forbidden.status_code == 403

    # Allowed player -> 200, second immediate attempt -> 429 cooldown
    with patch(
        "app.main.github_client.fetch_lichess_user_profile",
        return_value={"username": "Real_Vick"},
    ), patch(
        "app.main.github_client.trigger_workflow",
        return_value={"status": "dispatched", "username": "Real_Vick", "run": None},
    ):
        r1 = client.post("/api/pipeline/trigger", json={"username": "Real_Vick"})
        assert r1.status_code == 200

        r2 = client.post("/api/pipeline/trigger", json={"username": "Real_Vick"})
        assert r2.status_code == 429


def test_view_passcode_protection(monkeypatch):
    monkeypatch.setattr(config, "PORTAL_VIEW_PASSCODE", "secret-view-99")

    r_unauth = client.get("/api/players")
    assert r_unauth.status_code == 401

    r_cache_unauth = client.get("/api/cache/stats")
    assert r_cache_unauth.status_code == 401

    r_verify = client.post("/api/auth/verify", json={"token": "secret-view-99"})
    assert r_verify.status_code == 200
    assert r_verify.json()["view_authorized"] is True

    r_auth = client.get("/api/players", headers={"X-Portal-Auth": "secret-view-99"})
    assert r_auth.status_code == 200
    assert r_auth.headers.get("cache-control") == "private, max-age=180"


def test_dashboard_and_ssis_conditional_split():
    resp = client.get("/api/dashboard/Wizard-King?color=white&speed=bullet")
    assert resp.status_code == 200
    data = resp.json()
    assert data["empty"] is False
    assert data["summary"]["games"] > 0
    assert "white" in data["color_split"]
    assert "black" in data["color_split"]
    for g in data["recent_games"]["items"]:
        assert g["played_as"] == "white"
        assert g["speed"] == "bullet"

    csv_resp = client.get("/api/export/Wizard-King?color=black&speed=rapid")
    assert csv_resp.status_code == 200
    assert "game_id,rated,speed" in csv_resp.text


def test_multi_layer_caching_and_invalidation():
    warehouse._invalidate_dashboard_cache("Wizard-King")

    # First query computes from warehouse and populates _dashboard_cache
    r1 = client.get("/api/dashboard/Wizard-King?color=all&speed=blitz")
    assert r1.status_code == 200
    assert r1.headers.get("cache-control") == "public, max-age=180"
    assert r1.json()["cache_info"]["source"] in ("warehouse_compute", "drive_sync")

    # Second identical query hits server memory_cache
    r2 = client.get("/api/dashboard/Wizard-King?color=all&speed=blitz")
    assert r2.status_code == 200
    assert r2.json()["cache_info"]["source"] == "memory_cache"

    # Cache telemetry endpoint reports cached player and queries
    stats_resp = client.get("/api/cache/stats")
    assert stats_resp.status_code == 200
    stats = stats_resp.json()
    assert stats["cached_players_in_memory"] >= 1
    assert stats["cached_dashboard_queries"] >= 1
    assert "drive_calls_made" in stats
    assert "drive_calls_saved" in stats

    # Non-forced sync on fresh local player skips Drive metadata calls and invalidates query cache
    with patch.object(
        warehouse.drive, "get_player_files_metadata"
    ) as mock_meta:
        warehouse.sync_player("Wizard-King", force=False)
        mock_meta.assert_not_called()

    # Next query recomputes cleanly after invalidation
    r3 = client.get("/api/dashboard/Wizard-King?color=all&speed=blitz")
    assert r3.status_code == 200
    assert r3.json()["cache_info"]["source"] == "warehouse_compute"


def test_multipage_frontend_assets():
    idx = client.get("/")
    assert idx.status_code == 200
    assert 'data-page="overview"' in idx.text
    assert 'data-page="ratings"' in idx.text
    assert 'data-page="openings"' in idx.text
    assert 'data-page="timing"' in idx.text
    assert 'data-page="games"' in idx.text
    assert 'data-page="pipeline"' in idx.text

    css = client.get("/static/styles.css")
    assert css.status_code == 200
    assert ".report-page.active" in css.text

    js = client.get("/static/app.js")
    assert js.status_code == 200
    assert "navigateTo" in js.text
    assert "reportCache" in js.text

