import json
import pytest
from core.credentials import CredentialStore, CredentialStoreError


def _payload():
    return {"ozon": {"active": "one", "cabinets": {"one": {"client_id": "1", "api_key": "a"}, "two": {"client_id": "2", "api_key": "b"}}}}


def test_lockbox_json_is_authoritative_and_does_not_touch_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_MCP_CABINETS_JSON", json.dumps(_payload()))
    monkeypatch.delenv("MARKETPLACE_REQUIRE_REDIS", raising=False)
    s = CredentialStore(tmp_path / "cabinets.json")
    monkeypatch.setattr(s, "_state_redis", lambda: None)
    creds, source = s.resolve("ozon", ["client_id", "api_key"], {})
    assert creds == {"client_id": "1", "api_key": "a"}
    assert source == "one"
    assert not s.path.exists()


def test_invalid_lockbox_json_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_MCP_CABINETS_JSON", "{bad")
    with pytest.raises(CredentialStoreError):
        CredentialStore(tmp_path / "cabinets.json").list_cabinets("ozon")


def test_lockbox_credentials_are_immutable_from_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_MCP_CABINETS_JSON", json.dumps(_payload()))
    s = CredentialStore(tmp_path / "cabinets.json")
    with pytest.raises(CredentialStoreError):
        s.add_cabinet("ozon", "three", {"client_id": "3", "api_key": "c"})
    assert not s.path.exists()


def test_remote_active_cabinet_uses_shared_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_MCP_CABINETS_JSON", json.dumps(_payload()))
    state = {}
    class FakeRedis:
        def get(self, key): return state.get(key)
        def set(self, key, value): state[key] = value; return True
    s = CredentialStore(tmp_path / "cabinets.json")
    monkeypatch.setattr(s, "_state_redis", lambda: FakeRedis())
    assert s.set_active("ozon", "two") is True
    assert s.list_cabinets("ozon")["active"] == "two"
    creds, source = s.resolve("ozon", ["client_id", "api_key"], {})
    assert creds["client_id"] == "2" and source == "two"
