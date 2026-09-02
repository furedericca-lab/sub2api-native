import json
import asyncio
import sqlite3
from unittest import mock

import httpx

from backend.relay.state import RelayState
from backend.registration.account_key_crypto import AccountKeyCrypto
from backend.relay.router import RelayRouter
from backend.relay.proxy import _upstream_headers, forward_responses, stream_responses


def test_fresh_relay_state_has_runtime_only_schema_and_scheduler_policies(tmp_path):
    state = RelayState(tmp_path)
    assets = [
        {"account_id": 1, "site_key": "site-a", "origin": "https://a.test", "secret": "one"},
        {"account_id": 2, "site_key": "site-b", "origin": "https://b.test", "secret": "two"},
    ]
    state.update_models(1, ["gpt-test"])
    state.update_models(2, ["gpt-test"])
    with sqlite3.connect(state.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='relay_pool'"
        ).fetchone() is None
    assert [r["account_id"] for r in state.candidates("gpt-test", "fill_first", assets)] == [1, 2]
    assert [r["account_id"] for r in state.candidates("gpt-test", "round_robin", assets)] == [1, 2]
    assert [r["account_id"] for r in state.candidates("gpt-test", "round_robin", assets)] == [2, 1]
    assert [r["account_id"] for r in state.candidates("gpt-test", "fill_first", assets)] == [1, 2]


def test_legacy_pool_is_read_once_then_physically_removed(tmp_path):
    relay_root = tmp_path / "relay"
    relay_root.mkdir(parents=True, exist_ok=True)
    crypto = AccountKeyCrypto(tmp_path)
    path = relay_root / "relay_state.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
        PRAGMA user_version=2;
        CREATE TABLE relay_pool (
          account_id INTEGER PRIMARY KEY, profile_name TEXT NOT NULL,
          site_key TEXT NOT NULL, origin TEXT NOT NULL, key_ciphertext TEXT NOT NULL,
          key_id INTEGER NOT NULL DEFAULT 0, group_id INTEGER NOT NULL DEFAULT 0,
          enabled INTEGER NOT NULL DEFAULT 1, in_flight INTEGER NOT NULL DEFAULT 0,
          cooldown_until REAL NOT NULL DEFAULT 0, last_used_at REAL NOT NULL DEFAULT 0,
          last_status TEXT NOT NULL DEFAULT '', last_http_status INTEGER NOT NULL DEFAULT 0,
          models_json TEXT NOT NULL DEFAULT '[]', models_updated_at REAL NOT NULL DEFAULT 0
        );
        """)
        db.execute(
            "INSERT INTO relay_pool(account_id,profile_name,site_key,origin,key_ciphertext,key_id,group_id) VALUES(?,?,?,?,?,?,?)",
            (9, "legacy", "site", "https://legacy.test", crypto.encrypt("legacy-secret"), 44, 3),
        )
    state = RelayState(tmp_path)
    legacy = state.legacy_pool_rows()[0]
    assert crypto.decrypt(legacy["key_ciphertext"]) == "legacy-secret"
    state.finalize_legacy_pool_migration()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='relay_pool'"
        ).fetchone() is None


def test_relay_credential_is_hash_only(tmp_path):
    state = RelayState(tmp_path)
    credential = state.rotate_credential()
    assert state.authorize(credential)
    assert not state.authorize(credential + "x")
    assert credential.encode() not in state.meta_path.read_bytes()


def test_upstream_headers_keep_session_metadata_without_client_credentials():
    headers = _upstream_headers({"Authorization": "Bearer relay", "X-Api-Key": "relay", "X-Session-Id": "session", "X-Forwarded-For": "127.0.0.1"}, "upstream")
    assert headers["authorization"] == "Bearer upstream"
    assert headers["x-session-id"] == "session"
    assert list(name for name in headers if name == "content-type") == ["content-type"]
    assert all(name.lower() not in {"x-api-key", "x-forwarded-for"} for name in headers)


def test_session_affinity_keeps_existing_eligible_account(tmp_path):
    state = RelayState(tmp_path)
    assets = lambda: [
        {"account_id": 1, "site_key": "site-a", "origin": "https://a.test", "secret": "one"},
        {"account_id": 2, "site_key": "site-b", "origin": "https://b.test", "secret": "two"},
    ]
    state.update_models(1, ["gpt-test"])
    state.update_models(2, ["gpt-test"])
    state.bind_session("session-hash", 2)
    assert [r["account_id"] for r in __import__("backend.relay.router", fromlist=["RelayRouter"]).RelayRouter(state, assets).choose("gpt-test", "fill_first", "session-hash")] == [2, 1]


def test_http_response_is_never_replayed(tmp_path):
    state = RelayState(tmp_path); state.update_models(1, ["gpt-test"]); state.update_models(2, ["gpt-test"])
    assets = lambda: [{"account_id": 1, "site_key": "a", "origin": "https://a.test", "secret": "one"}, {"account_id": 2, "site_key": "b", "origin": "https://b.test", "secret": "two"}]
    client = mock.AsyncMock(); client.__aenter__.return_value = client; client.post.return_value = httpx.Response(503, json={"error": "failed"})
    with mock.patch("backend.relay.proxy.httpx.AsyncClient", return_value=client):
        response, error = asyncio.run(forward_responses(RelayRouter(state, assets), "gpt-test", b'{}', "fill_first"))
    assert error is None and response.status_code == 503
    assert client.post.await_count == 1
    assert len(state.requests()) == 1 and state.requests()[0]["outcome"] == "upstream_error"


def test_post_write_transport_error_is_not_replayed_and_is_logged(tmp_path):
    state = RelayState(tmp_path); state.update_models(1, ["gpt-test"]); state.update_models(2, ["gpt-test"])
    assets = lambda: [{"account_id": 1, "site_key": "a", "origin": "https://a.test", "secret": "one"}, {"account_id": 2, "site_key": "b", "origin": "https://b.test", "secret": "two"}]
    client = mock.AsyncMock(); client.__aenter__.return_value = client; client.post.side_effect = httpx.ReadTimeout("after write")
    with mock.patch("backend.relay.proxy.httpx.AsyncClient", return_value=client):
        response, error = asyncio.run(forward_responses(RelayRouter(state, assets), "gpt-test", b'{}', "fill_first"))
    assert response is None and error["error"]["code"] == "upstream_transport_error"
    assert client.post.await_count == 1
    assert len(state.requests()) == 1 and state.requests()[0]["outcome"] == "transport_error"


def test_stream_header_transport_error_is_not_replayed_and_is_logged(tmp_path):
    state = RelayState(tmp_path); state.update_models(1, ["gpt-test"]); state.update_models(2, ["gpt-test"])
    assets = lambda: [{"account_id": 1, "site_key": "a", "origin": "https://a.test", "secret": "one"}, {"account_id": 2, "site_key": "b", "origin": "https://b.test", "secret": "two"}]
    client = mock.MagicMock()
    client.build_request.return_value = httpx.Request("POST", "https://a.test/v1/responses")
    client.send = mock.AsyncMock(side_effect=httpx.ReadTimeout("after write"))
    client.aclose = mock.AsyncMock()
    with mock.patch("backend.relay.proxy.httpx.AsyncClient", return_value=client):
        response, chunks, error = asyncio.run(stream_responses(RelayRouter(state, assets), "gpt-test", b'{}', "fill_first"))
    assert response is None and chunks is None and error["error"]["code"] == "upstream_transport_error"
    assert client.send.await_count == 1
    assert len(state.requests()) == 1 and state.requests()[0]["outcome"] == "transport_error"
