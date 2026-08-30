"""Multi-cabinet credential store for local and remote deployments.

Local installs keep credentials in ``~/.marketplace-mcp/cabinets.json``.
Remote deployments may inject the entire cabinet map through
``MARKETPLACE_MCP_CABINETS_JSON`` (normally from Yandex Lockbox). When that
environment source is present it is authoritative and immutable: secret writes
through MCP tools are refused. Only the selected cabinet name is mutable, and
in production it is stored in the same shared Redis/Valkey used by the global
rate limiter so every replica sees one active cabinet per service.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

STORE_DIR = Path(os.environ.get("MARKETPLACE_MCP_HOME", Path.home() / ".marketplace-mcp"))
STORE_PATH = STORE_DIR / "cabinets.json"
ENV_CABINETS = "MARKETPLACE_MCP_CABINETS_JSON"


class CredentialStoreError(RuntimeError):
    """Credential state is unavailable or a deployment-managed mutation was attempted."""


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


class CredentialStore:
    """Resolve named marketplace credentials without ever returning secret listings."""

    def __init__(self, path: Path = STORE_PATH):
        self.path = path

    def _env_data(self) -> Optional[dict]:
        raw = os.environ.get(ENV_CABINETS, "")
        if not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CredentialStoreError(f"{ENV_CABINETS} contains invalid JSON") from exc
        if not isinstance(data, dict):
            raise CredentialStoreError(f"{ENV_CABINETS} root must be a JSON object")
        return data

    def _env_mode(self) -> bool:
        return bool(os.environ.get(ENV_CABINETS, "").strip())

    @staticmethod
    def _state_key(service: str) -> str:
        return f"marketplace-cabinets:v1:{service}:active"

    def _state_redis(self):
        from redis import Redis
        from .rate_limit import redis_connection_kwargs, redis_url_from_env

        url = redis_url_from_env()
        if not url:
            if _truthy("MARKETPLACE_REQUIRE_REDIS"):
                raise CredentialStoreError("shared Redis/Valkey is required but not configured")
            return None
        try:
            return Redis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
                **redis_connection_kwargs(url),
            )
        except Exception as exc:  # noqa: BLE001
            raise CredentialStoreError(
                f"shared Redis/Valkey state backend unavailable: {type(exc).__name__}"
            ) from exc

    def _active_override(self, service: str) -> Optional[str]:
        if not self._env_mode():
            return None
        client = self._state_redis()
        if client is None:
            return None
        try:
            value = client.get(self._state_key(service))
            return str(value) if value else None
        except Exception as exc:  # noqa: BLE001
            if _truthy("MARKETPLACE_REQUIRE_REDIS"):
                raise CredentialStoreError(
                    f"shared cabinet state unavailable: {type(exc).__name__}"
                ) from exc
            return None

    def _set_active_override(self, service: str, name: str) -> None:
        client = self._state_redis()
        if client is None:
            raise CredentialStoreError("remote cabinet selection requires shared Redis/Valkey")
        try:
            client.set(self._state_key(service), name)
        except Exception as exc:  # noqa: BLE001
            raise CredentialStoreError(
                f"shared cabinet state unavailable: {type(exc).__name__}"
            ) from exc

    def _ensure_dir(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass

    def _load_local(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            self._backup_corrupt(raw)
            return {}
        if not isinstance(data, dict):
            self._backup_corrupt(raw)
            return {}
        return data

    def _load(self) -> dict:
        env = self._env_data()
        return env if env is not None else self._load_local()

    def _backup_corrupt(self, raw: str) -> None:
        try:
            self._ensure_dir()
            n = 0
            while True:
                dest = self.path.with_name(f"{self.path.name}.corrupt-{n}")
                if not dest.exists():
                    break
                n += 1
            dest.write_text(raw, encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(dest, 0o600)
            print(
                f"[marketplace-mcp] WARNING: {self.path} was unreadable; backed it up "
                f"to {dest} and started a fresh store. Your old keys are preserved.",
                file=sys.stderr,
            )
        except OSError:
            pass

    def _save(self, data: dict) -> None:
        if self._env_mode():
            raise CredentialStoreError(
                "cabinet credentials are deployment-managed via Lockbox and cannot be changed from MCP"
            )
        self._ensure_dir()
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp"
        )
        try:
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, ensure_ascii=False, indent=2))
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def _locked(self):
        self._ensure_dir()
        lock_path = self.path.with_name(self.path.name + ".lock")
        lock_fd = None
        try:
            if fcntl is not None:
                lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        finally:
            if lock_fd is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)

    def _mutate(self, fn: Callable[[dict], object]) -> object:
        if self._env_mode():
            raise CredentialStoreError(
                "cabinet credentials are deployment-managed via Lockbox and cannot be changed from MCP"
            )
        with self._locked():
            data = self._load_local()
            result = fn(data)
            self._save(data)
            return result

    def list_cabinets(self, service: str) -> dict:
        svc = self._load().get(service, {})
        if not isinstance(svc, dict):
            svc = {}
        cabs = svc.get("cabinets") or {}
        if not isinstance(cabs, dict):
            cabs = {}
        active = self._active_override(service) or svc.get("active")
        if active not in cabs:
            active = next(iter(cabs), None)
        return {"active": active, "cabinets": sorted(cabs.keys())}

    def add_cabinet(self, service: str, name: str, creds: dict,
                    make_active: bool = True) -> None:
        def apply(data: dict) -> None:
            svc = data.setdefault(service, {"active": None, "cabinets": {}})
            svc.setdefault("cabinets", {})[name] = creds
            if make_active or not svc.get("active"):
                svc["active"] = name
        self._mutate(apply)

    def remove_cabinet(self, service: str, name: str) -> bool:
        def apply(data: dict) -> bool:
            svc = data.get(service, {})
            cabs = svc.get("cabinets", {})
            if name not in cabs:
                return False
            del cabs[name]
            if svc.get("active") == name:
                svc["active"] = next(iter(cabs), None)
            return True
        return bool(self._mutate(apply))

    def set_active(self, service: str, name: str) -> bool:
        svc = self._load().get(service, {})
        cabs = svc.get("cabinets", {}) if isinstance(svc, dict) else {}
        if name not in cabs:
            return False
        if self._env_mode():
            self._set_active_override(service, name)
            return True

        def apply(data: dict) -> bool:
            local_svc = data.get(service, {})
            if name not in (local_svc.get("cabinets") or {}):
                return False
            local_svc["active"] = name
            return True
        return bool(self._mutate(apply))

    def resolve(self, service: str, fields: list[str],
                env_map: dict[str, str]) -> tuple[dict, str]:
        data = self._load()
        svc = data.get(service, {}) if isinstance(data, dict) else {}
        if not isinstance(svc, dict):
            svc = {}
        cabs = svc.get("cabinets") or {}
        if not isinstance(cabs, dict):
            cabs = {}
        active = self._active_override(service) or svc.get("active")
        if active and active in cabs and isinstance(cabs[active], dict):
            stored = cabs[active]
            creds = {f: stored.get(f, "") for f in fields}
            if all(creds[f] for f in fields):
                return creds, str(active)

        env_creds = {f: os.environ.get(env_map.get(f, ""), "") for f in fields}
        if all(env_creds[f] for f in fields):
            return env_creds, "env"

        merged = {
            f: (cabs.get(active, {}).get(f, "") if active and isinstance(cabs.get(active), dict) else "")
            or env_creds.get(f, "")
            for f in fields
        }
        source = str(active) if active in cabs else ("env" if any(env_creds.values()) else "none")
        return merged, source

    def missing(self, service: str, fields: list[str],
                env_map: dict[str, str]) -> list[str]:
        creds, _ = self.resolve(service, fields, env_map)
        return [f for f in fields if not creds.get(f)]
