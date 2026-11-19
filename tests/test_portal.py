from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import access_guard, app, github_client, warehouse
from app.warehouse import _sanitize_csv_cell


@pytest.fixture(autouse=True)
def reset_security_state(monkeypatch):
    monkeypatch.setattr(config, "PORTAL_TRIGGER_PASSCODE", "")
    monkeypatch.setattr(config, "PORTAL_USER_TOKENS", "")
    monkeypatch.setattr(config, "PORTAL_VIEW_PASSCODE", "")
    monkeypatch.setattr(config, "ALLOWED_TRIGGER_PLAYERS", "")
    monkeypatch.setattr(config, "TRIGGER_COOLDOWN_SECONDS", 120)
    monkeypatch.setattr(config, "TRIGGER_MAX_PER_HOUR", 10)
    monkeypatch.setattr(config, "TRUST_PROXY_HEADERS", True)
    access_guard._player_last_trigger.clear()
    access_guard._ip_trigger_timestamps.clear()
    access_guard._in_flight_triggers.clear()
    yield


client = TestClient(app)


def test_health_auth_and_security_headers():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["trigger_protected"] is False
    # ARCH-04 security headers verification
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert (
        resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    )
    assert "default-src 'self'" in resp.headers.get("content-security-policy", "")

    auth = client.get("/api/auth/status")
    assert auth.status_code == 200
    assert auth.json()["trigger_authorized"] is True
    assert auth.json()["view_authorized"] is True


def test_trigger_requires_token_when_protected(monkeypatch):
    monkeypatch.setattr(config, "PORTAL_USER_TOKENS", "kingsley:key-123,guest:key-456")

    resp = client.post("/api/pipeline/trigger", json={"username": "Real_Vick"})
    assert resp.status_code == 401

    resp_bad = client.post(
        "/api/pipeline/trigger",
        json={"username": "Real_Vick"},
        headers={"X-Portal-Auth": "wrong-key"},
    )
    assert resp_bad.status_code == 401

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

    resp_forbidden = client.post(
        "/api/pipeline/trigger", json={"username": "MagnusCarlsen"}
    )
    assert resp_forbidden.status_code == 403

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


def test_failed_trigger_rolls_back_cooldown_and_quota(monkeypatch):
    """SEC-03 regression: failed Lichess lookup or dispatch must not burn player cooldown."""
    monkeypatch.setattr(config, "TRIGGER_COOLDOWN_SECONDS", 120)

    with patch(
        "app.main.github_client.fetch_lichess_user_profile",
        side_effect=ValueError("Lichess user 'Real_Vick' not found."),
    ):
        r_fail = client.post("/api/pipeline/trigger", json={"username": "Real_Vick"})
        assert r_fail.status_code == 404

    # Subsequent valid trigger for the same username must NOT be blocked by 429 cooldown
    with patch(
        "app.main.github_client.fetch_lichess_user_profile",
        return_value={"username": "Real_Vick"},
    ), patch(
        "app.main.github_client.trigger_workflow",
        return_value={"status": "dispatched", "username": "Real_Vick", "run": None},
    ):
        r_ok = client.post("/api/pipeline/trigger", json={"username": "Real_Vick"})
        assert r_ok.status_code == 200


def test_proxy_forwarded_for_rate_limit_isolation(monkeypatch):
    """SEC-03 regression: users with distinct X-Forwarded-For IPs do not share hourly quota."""
    monkeypatch.setattr(config, "TRIGGER_MAX_PER_HOUR", 1)
    monkeypatch.setattr(config, "TRIGGER_COOLDOWN_SECONDS", 0)

    with patch(
        "app.main.github_client.fetch_lichess_user_profile",
        return_value={"username": "Real_Vick"},
    ), patch(
        "app.main.github_client.trigger_workflow",
        return_value={"status": "dispatched", "username": "Real_Vick", "run": None},
    ):
        r_ip1 = client.post(
            "/api/pipeline/trigger",
            json={"username": "Real_Vick"},
            headers={"X-Forwarded-For": "203.0.113.10"},
        )
        assert r_ip1.status_code == 200

        r_ip1_again = client.post(
            "/api/pipeline/trigger",
            json={"username": "Real_Vick"},
            headers={"X-Forwarded-For": "203.0.113.10"},
        )
        assert r_ip1_again.status_code == 429

        # Different client IP behind same proxy still has quota
        r_ip2 = client.post(
            "/api/pipeline/trigger",
            json={"username": "Real_Vick"},
            headers={"X-Forwarded-For": "198.51.100.22"},
        )
        assert r_ip2.status_code == 200


def test_username_validation_blocks_injection_and_phantom_dirs():
    """SEC-01 & SEC-02 regression: reject shell metacharacters and do not create directories for unknown users."""
    for bad_name in ["user;rm -rf /", "user$(id)", "../secret", "a"]:
        r_trig = client.post("/api/pipeline/trigger", json={"username": bad_name})
        assert r_trig.status_code == 400

        r_dash = client.get(f"/api/dashboard/{bad_name}")
        assert r_dash.status_code in (400, 404)

    # Valid-format username that does not exist in Drive must not create a directory on disk
    phantom = "NonExistentPlayer_999"
    phantom_dir = config.DATA_CACHE_DIR / phantom
    assert not phantom_dir.exists()
    with patch.object(warehouse, "discover_players", return_value=[]):
        res = warehouse.sync_player(phantom, force=False)
        assert res["total_games"] == 0
        assert not phantom_dir.exists()


def test_view_passcode_protection_and_no_url_query_token(monkeypatch):
    """SEC-06 regression: URL ?token= query parameter must not grant access."""
    monkeypatch.setattr(config, "PORTAL_VIEW_PASSCODE", "secret-view-99")

    r_unauth = client.get("/api/players")
    assert r_unauth.status_code == 401

    r_query_leak = client.get("/api/players?token=secret-view-99")
    assert r_query_leak.status_code == 401

    r_cache_unauth = client.get("/api/cache/stats")
    assert r_cache_unauth.status_code == 401

    r_verify = client.post("/api/auth/verify", json={"token": "secret-view-99"})
    assert r_verify.status_code == 200
    assert r_verify.json()["view_authorized"] is True

    r_auth = client.get("/api/players", headers={"X-Portal-Auth": "secret-view-99"})
    assert r_auth.status_code == 200
    assert r_auth.headers.get("cache-control") == "private, max-age=180"


def test_dashboard_ssis_split_and_csv_sanitization():
    resp = client.get("/api/dashboard/Wizard-King?color=white&speed=bullet")
    assert resp.status_code == 200
    data = resp.json()
    assert data["empty"] is False
    assert data["summary"]["games"] > 0
    # BUG-01 legacy repair: non-tournament pool/lobby/friend games are not counted as tournament games
    assert data["summary"]["tournament_games"] < data["summary"]["games"]
    assert "white" in data["color_split"]
    assert "black" in data["color_split"]
    for g in data["recent_games"]["items"]:
        assert g["played_as"] == "white"
        assert g["speed"] == "bullet"

    # SEC-05: Content-Disposition header sanitization & CSV formula prefix neutralization
    assert _sanitize_csv_cell("=CMD|' /C calc'!A0") == "'=CMD|' /C calc'!A0"
    assert _sanitize_csv_cell("+SUM(A1:A2)") == "'+SUM(A1:A2)"
    assert _sanitize_csv_cell("@惡意") == "'@惡意"
    assert _sanitize_csv_cell("Caro-Kann Defense") == "Caro-Kann Defense"

    csv_resp = client.get(
        "/api/export/Wizard-King",
        params={"color": 'black"evil', "speed": "rapid\r\nInjected:1"},
    )
    assert csv_resp.status_code == 200
    disp = csv_resp.headers.get("content-disposition", "")
    assert "\r" not in disp and "\n" not in disp
    assert 'filename="lichess_Wizard-King_blackevil_rapidInjected1.csv"' in disp
    assert "game_id,rated,speed" in csv_resp.text


def test_multi_layer_caching_pagination_reuse_and_invalidation():
    """PERF-01 regression: pagination (page=2) hits slice memory_cache without recomputing analytics."""
    warehouse._invalidate_dashboard_cache("Wizard-King")

    r1 = client.get("/api/dashboard/Wizard-King?color=all&speed=blitz&page=1&page_size=10")
    assert r1.status_code == 200
    assert r1.headers.get("cache-control") == "public, max-age=180"
    assert r1.json()["cache_info"]["source"] in ("warehouse_compute", "drive_sync")
    page1_ids = [g["game_id"] for g in r1.json()["recent_games"]["items"]]

    # Page 2 with same filters must hit memory_cache directly (PERF-01) and return different games
    r_page2 = client.get(
        "/api/dashboard/Wizard-King?color=all&speed=blitz&page=2&page_size=10"
    )
    assert r_page2.status_code == 200
    assert r_page2.json()["cache_info"]["source"] == "memory_cache"
    page2_ids = [g["game_id"] for g in r_page2.json()["recent_games"]["items"]]
    assert page1_ids != page2_ids

    stats_resp = client.get("/api/cache/stats")
    assert stats_resp.status_code == 200
    stats = stats_resp.json()
    assert stats["cached_players_in_memory"] >= 1
    assert stats["cached_dashboard_queries"] >= 1

    with patch.object(warehouse.drive, "get_player_files_metadata") as mock_meta:
        warehouse.sync_player("Wizard-King", force=False)
        mock_meta.assert_not_called()

    r3 = client.get("/api/dashboard/Wizard-King?color=all&speed=blitz&page=1&page_size=10")
    assert r3.status_code == 200
    assert r3.json()["cache_info"]["source"] == "warehouse_compute"


def test_single_flight_run_auto_sync_and_lru_memory_bound(monkeypatch):
    """CONC-02 & PERF-02 regression: single-flight run sync lock and LRU DataFrame eviction."""
    run_id = 987654321
    github_client._run_map.pop(str(run_id), None)
    assert github_client.try_begin_run_sync(run_id) is True
    # Concurrent second poll while sync is in flight must return False
    assert github_client.try_begin_run_sync(run_id) is False
    github_client.finish_run_sync(run_id, username="Real_Vick", succeeded=True)
    # After completion, synced_after_completion is True so subsequent polls also return False
    assert github_client.try_begin_run_sync(run_id) is False

    # PERF-02: test LRU cap on _games_cache
    monkeypatch.setattr(config, "MAX_CACHED_PLAYERS_IN_MEMORY", 4)
    for i in range(6):
        warehouse._store_player_frames(f"TestUser_{i}", pd.DataFrame(), pd.DataFrame())
    assert len(warehouse._games_cache) <= 4
    assert "TestUser_0" not in warehouse._games_cache
    assert "TestUser_5" in warehouse._games_cache


def test_multipage_frontend_assets():
    idx = client.get("/")
    assert idx.status_code == 200
    for page_name in ("overview", "ratings", "openings", "timing", "games", "pipeline"):
        assert f'data-page="{page_name}"' in idx.text

    css = client.get("/static/styles.css")
    assert css.status_code == 200
    assert ".report-page.active" in css.text

    js = client.get("/static/app.js")
    assert js.status_code == 200
    assert "AbortController" in js.text
    assert "setClientReportCache" in js.text
