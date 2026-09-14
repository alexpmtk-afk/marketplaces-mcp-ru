from pathlib import Path

p = Path('core/archive_google.py')
s = p.read_text(encoding='utf-8')

start_doc = s.index('"""')
end_doc = s.index('"""', start_doc + 3) + 3
new_doc = '''"""Canonical Marketplaces Google Drive archive backend using Bridge Protocol v1.

The application runtime has one Google Drive transport contract only: shared
Bridge Protocol v1, project_id=marketplaces, release 1.0.0. Rollback belongs to
cloud revision/deployment history, not to a second protocol path in this module.
"""'''
s = new_doc + s[end_doc:]

s = s.replace('BRIDGE_PROTOCOL_V1 = 1\n_LEGACY_MODE = "legacy"\n_V1_MODES = {"1", "v1", "protocol-v1"}\n', 'BRIDGE_PROTOCOL_V1 = 1\nBRIDGE_RELEASE = "1.0.0"\n')
s = s.replace('    """Async Marketplaces client for legacy Bridge v3 or shared Protocol v1."""', '    """Async Marketplaces client for stable shared Bridge Protocol v1."""')
s = s.replace('        protocol_version: int = 0,', '        protocol_version: int = BRIDGE_PROTOCOL_V1,')
s = s.replace('        if self.protocol_version not in {0, BRIDGE_PROTOCOL_V1}:\n            raise ArchiveStorageNotConfigured(\n                f"Unsupported Google Drive bridge protocol: {self.protocol_version}"\n            )\n        if self.protocol_version == BRIDGE_PROTOCOL_V1 and self.project_id != MARKETPLACES_BRIDGE_PROJECT_ID:\n', '        if self.protocol_version != BRIDGE_PROTOCOL_V1:\n            raise ArchiveStorageNotConfigured("Marketplaces archive runtime supports Bridge Protocol v1 only")\n        if self.project_id != MARKETPLACES_BRIDGE_PROJECT_ID:\n')
s = s.replace('    def is_protocol_v1(self) -> bool:\n        return self.protocol_version == BRIDGE_PROTOCOL_V1\n', '    def is_protocol_v1(self) -> bool:\n        return True\n')

from_start = s.index('    @classmethod\n    def from_env')
path_start = s.index('    @staticmethod\n    def _path', from_start)
new_from = '''    @classmethod
    def from_env(cls) -> "GoogleDriveArchiveStore":
        url = os.environ.get("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_URL", "").strip()
        secret = os.environ.get("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_SECRET", "").strip()
        root = os.environ.get("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_ROOT_ID", "").strip()
        if not url or not secret or not root:
            raise ArchiveStorageNotConfigured(
                "Set MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_URL, "
                "MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_SECRET and "
                "MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_ROOT_ID"
            )
        return cls(
            bridge_url=url,
            bridge_secret=secret,
            root_folder_id=root,
            protocol_version=BRIDGE_PROTOCOL_V1,
            project_id=MARKETPLACES_BRIDGE_PROJECT_ID,
        )

'''
s = s[:from_start] + new_from + s[path_start:]

legacy_start = s.index('    async def _post_legacy')
v1_start = s.index('    async def _post_v1', legacy_start)
s = s[:legacy_start] + s[v1_start:]

call_start = s.index('    async def _call')
ensure_start = s.index('    async def ensure_folder_path', call_start)
new_call = '''    async def _call(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._post_v1(action, payload, idempotency_key=idempotency_key)

'''
s = s[:call_start] + new_call + s[ensure_start:]

s = s.replace('        idem = None\n        if self.is_protocol_v1:\n            idem = self._stable_idempotency_key("trash_by_id", str(file_id))\n', '        idem = self._stable_idempotency_key("trash_by_id", str(file_id))\n')
s = s.replace('        effective_staging_name = str(staging_name)\n        if self.is_protocol_v1:\n            current = await self.file_metadata(str(file_id))\n            if str(current.get("name") or "") and str(current.get("name")) != str(canonical_name):\n                effective_staging_name = str(current["name"])\n', '        effective_staging_name = str(staging_name)\n        current = await self.file_metadata(str(file_id))\n        if str(current.get("name") or "") and str(current.get("name")) != str(canonical_name):\n            effective_staging_name = str(current["name"])\n')
s = s.replace('        idem = None\n        if self.is_protocol_v1:\n            idem = self._stable_idempotency_key(\n                "promote_verified",\n                payload["path"],\n                payload["file_id"],\n                payload["canonical_filename"],\n                payload["expected_bytes"],\n                payload["expected_sha256"],\n                payload["previous_file_id"],\n            )\n', '        idem = self._stable_idempotency_key(\n            "promote_verified",\n            payload["path"],\n            payload["file_id"],\n            payload["canonical_filename"],\n            payload["expected_bytes"],\n            payload["expected_sha256"],\n            payload["previous_file_id"],\n        )\n')
s = s.replace('        idem = None\n        if self.is_protocol_v1:\n            idem = self._stable_idempotency_key(\n                "resumable_start",\n                payload["path"],\n                payload["filename"],\n                payload["mime_type"],\n                payload["total_bytes"],\n            )\n', '        idem = self._stable_idempotency_key(\n            "resumable_start",\n            payload["path"],\n            payload["filename"],\n            payload["mime_type"],\n            payload["total_bytes"],\n        )\n')

for text in (
'''        if not self.is_protocol_v1:
            raise ArchiveStorageError(
                "Large-download capability is only defined by shared Bridge Protocol v1",
                code="LARGE_DOWNLOAD_PROTOCOL_REQUIRED",
            )
''',
):
    s = s.replace(text, '')

# Replace v1/legacy download_bytes with the single verified large-download path.
db_start = s.index('    async def download_bytes')
dn_start = s.index('    async def download_named', db_start)
new_db = '''    async def download_bytes(self, file_id: str) -> bytes:
        _, raw = await self.download_large_by_id(str(file_id))
        return raw

'''
s = s[:db_start] + new_db + s[dn_start:]

# Keep only the Protocol-v1 download_named implementation.
dn_start = s.index('    async def download_named')
upload_start = s.index('    async def upload_bytes', dn_start)
dn = s[dn_start:upload_start]
if '        if self.is_protocol_v1:\n' not in dn or '\n        data = await self._post_legacy(' not in dn:
    raise SystemExit('download_named legacy/v1 structure not found')
header, rest = dn.split('        if self.is_protocol_v1:\n', 1)
v1_body, _legacy = rest.split('\n        data = await self._post_legacy(', 1)
# de-indent the retained body by four spaces
v1_body = ''.join(line[4:] if line.startswith('    ') else line for line in v1_body.splitlines(True))
s = s[:dn_start] + header + v1_body.rstrip() + '\n\n' + s[upload_start:]

# Replace upload_bytes with v1-only implementation while preserving signature.
upload_start = s.index('    async def upload_bytes')
status_start = s.index('    async def status', upload_start)
upload = s[upload_start:status_start]
if '        if self.is_protocol_v1:\n' not in upload or '\n        else:\n            result = await self._post_legacy(' not in upload:
    raise SystemExit('upload_bytes legacy/v1 structure not found')
pre, body = upload.split('        if self.is_protocol_v1:\n', 1)
v1_body, tail_legacy = body.split('\n        else:\n            result = await self._post_legacy(', 1)
# locate common verification tail after legacy call
marker = '\n        returned_sha = str(result.get("sha256", "")).lower()'
if marker not in tail_legacy:
    raise SystemExit('upload common tail not found')
_legacy_call, common = tail_legacy.split(marker, 1)
v1_body = ''.join(line[4:] if line.startswith('    ') else line for line in v1_body.splitlines(True))
common = marker + common
common = common.replace('        if self.is_protocol_v1 and item.sha256_checksum and item.sha256_checksum.lower() != sha256:', '        if item.sha256_checksum and item.sha256_checksum.lower() != sha256:')
s = s[:upload_start] + pre + v1_body.rstrip() + common + '\n\n' + s[status_start:]

# Replace status with stable Protocol-v1 health only.
status_start = s.index('    async def status')
builder_start = s.index('\n\ndef build_google_archive_store_from_env', status_start)
new_status = '''    async def status(self) -> dict[str, Any]:
        data = await self._post_v1("health")
        actual_root = str(data.get("root_id", ""))
        root_name = str(data.get("root_name", ""))
        capabilities = dict(data.get("capabilities") or {})
        if int(data.get("protocol_version") or 0) != BRIDGE_PROTOCOL_V1:
            raise ArchiveStorageError("Bridge v1 deep health protocol mismatch", code="PROTOCOL_MISMATCH")
        if str(data.get("bridge_release") or "") != BRIDGE_RELEASE:
            raise ArchiveStorageError(
                f"Bridge release mismatch: {data.get('bridge_release')!r} != {BRIDGE_RELEASE!r}",
                code="BRIDGE_RELEASE_MISMATCH",
            )
        if str(data.get("project_id") or "") != MARKETPLACES_BRIDGE_PROJECT_ID:
            raise ArchiveStorageError("Bridge v1 deep health project mismatch", code="PROJECT_MISMATCH")
        if actual_root != self.root_folder_id:
            raise ArchiveStorageError(
                f"Bridge v1 root mismatch: {actual_root!r} != {self.root_folder_id!r}",
                code="ROOT_MISMATCH",
            )
        if capabilities.get("fixed_root_file_id_guard") is not True:
            raise ArchiveStorageError("Bridge v1 fixed-root file-id protection is required", code="ROOT_GUARD_REQUIRED")
        if capabilities.get("idempotent_mutations") is not True:
            raise ArchiveStorageError("Bridge v1 idempotent mutations are required", code="IDEMPOTENCY_REQUIRED")
        if capabilities.get("drive_resumable_upload") is not True:
            raise ArchiveStorageError("Bridge v1 resumable upload is required", code="RESUMABLE_REQUIRED")
        if capabilities.get("drive_large_download") is not True or capabilities.get("drive_large_download_transport") != "drive_files_download_lro":
            raise ArchiveStorageError("Bridge v1 verified large download is required", code="LARGE_DOWNLOAD_REQUIRED")
        if capabilities.get("global_script_lock") is not False:
            raise ArchiveStorageError("Bridge v1 global ScriptLock must be disabled", code="GLOBAL_LOCK_FORBIDDEN")
        return {
            "configured": True,
            "reachable": True,
            "backend": "google_drive_bridge_v1",
            "root_folder_id": actual_root,
            "root_name": root_name,
            "bridge_protocol_version": BRIDGE_PROTOCOL_V1,
            "bridge_release": BRIDGE_RELEASE,
            "project_id": MARKETPLACES_BRIDGE_PROJECT_ID,
            "capabilities": capabilities,
            "large_download_ready": True,
        }
'''
s = s[:status_start] + new_status + s[builder_start:]

# No application-level legacy tokens may remain.
for forbidden in ('_post_legacy', '_LEGACY_MODE', '_V1_MODES', 'google_drive_apps_script_bridge', 'bridge_protocol_mode'):
    if forbidden in s:
        raise SystemExit(f'legacy token remains in runtime: {forbidden}')
p.write_text(s, encoding='utf-8')

# Final .env documents only the canonical v1 route.
env = Path('.env.example')
e = env.read_text(encoding='utf-8')
start = e.index('# --- Canonical Google Drive archive via Apps Script bridge ---')
end = e.index('# --- Durable queue/staging', start)
block = '''# --- Canonical Google Drive archive via shared Bridge Protocol v1 ---
# project_id is pinned in code to `marketplaces`; use only the isolated
# Marketplaces Apps Script deployment and its project Lockbox secret.
MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_URL=
MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_SECRET=
MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_ROOT_ID=

'''
e = e[:start] + block + e[end:]
env.write_text(e, encoding='utf-8')

# Rewrite the v1 core test's environment contract and release labels.
test = Path('tests/test_archive_google_bridge_v1.py')
t = test.read_text(encoding='utf-8')
t = t.replace('"bridge_release": "1.0.0-alpha.1"', '"bridge_release": "1.0.0"')
t = t.replace('"bridge_release": "1.0.0-alpha.2"', '"bridge_release": "1.0.0"')
old_test_start = t.index('def test_protocol_v1_is_opt_in_and_requires_dedicated_env')
next_test = t.index('\ndef test_protocol_v1_rejects_non_marketplaces_project_id', old_test_start)
new_test = '''def test_protocol_v1_is_the_only_runtime_env_contract(monkeypatch):
    for name in (
        "MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_URL",
        "MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_SECRET",
        "MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_ROOT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    # Legacy variables must not configure the canonical client.
    monkeypatch.setenv("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_URL", "https://script.google.com/macros/s/legacy/exec")
    monkeypatch.setenv("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_SECRET", "legacy-secret")
    monkeypatch.setenv("MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID", "legacy-root")
    with pytest.raises(Exception, match="BRIDGE_V1"):
        GoogleDriveArchiveStore.from_env()

    monkeypatch.setenv("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_URL", V1_URL)
    monkeypatch.setenv("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_SECRET", "new-secret")
    monkeypatch.setenv("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_ROOT_ID", ROOT_ID)
    v1 = GoogleDriveArchiveStore.from_env()
    assert v1.is_protocol_v1 is True
    assert v1.protocol_version == 1
    assert v1.project_id == "marketplaces"
    assert v1.bridge_url == V1_URL

'''
t = t[:old_test_start] + new_test + t[next_test+1:]
test.write_text(t, encoding='utf-8')

# Stable test is the former alpha2 coverage with stable naming; source file is deleted separately.
alpha = Path('tests/test_archive_google_bridge_v1_alpha2.py')
a = alpha.read_text(encoding='utf-8')
a = a.replace('alpha2', 'stable').replace('Alpha2', 'Stable').replace('ALPHA2', 'STABLE')
Path('tests/test_archive_google_bridge_v1_stable.py').write_text(a, encoding='utf-8')

# Canonical integration note.
Path('docs/SHARED_GOOGLE_DRIVE_BRIDGE_V1.md').write_text('''# Marketplaces → Google Drive Bridge Protocol v1\n\nStatus: **canonical application runtime**.\n\nThe Marketplaces application has one Google Drive protocol path:\n\n`Marketplaces MCP → project queue/locks → Protocol v1 client → isolated Marketplaces Apps Script 1.0.0 → fixed root MCP архив базы данных`.\n\nRequired runtime variables:\n- `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_URL`;\n- `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_SECRET`;\n- `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_ROOT_ID`.\n\n`project_id=marketplaces` is hard-pinned in code. Every mutation uses a stable idempotency key and every request has a unique request id. Large uploads use Drive resumable upload and large downloads use the Bridge-brokered Drive files.download LRO with exact size/SHA256 verification.\n\nThere is no application-level legacy/v3 fallback. Operational rollback means switching the Yandex Serverless Container to the previous known-good revision/image; it does not mean selecting a second protocol inside the current code.\n\nCentral protocol source of truth: `alexpmtk-afk/mcp-yandex-cloud-infra/shared/google-drive-bridge/`, release `1.0.0`.\n''', encoding='utf-8')

print('MARKETPLACES_BRIDGE_V1_FINAL_PATCH=PASS')
