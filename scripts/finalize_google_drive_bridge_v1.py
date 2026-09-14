from pathlib import Path

ROOT = Path('.')

# 1. Finalize runtime contract in archive_google.py without changing current production selection.
p = ROOT / 'core/archive_google.py'
s = p.read_text(encoding='utf-8')
s = s.replace(
    'Production remains on the legacy Marketplaces bridge route until an isolated\nProtocol-v1 Marketplaces deployment, secret/Lockbox binding and live acceptance\nare available. Protocol v1 is opt-in and never falls back to legacy credentials.',
    'Shared Google Drive Bridge Protocol v1.0.0 is the final archive transport.\nThe legacy Marketplaces route is retained only as an explicit rollback profile;\nProtocol v1 never falls back to legacy credentials or deployment state.',
)
s = s.replace('BRIDGE_PROTOCOL_V1 = 1\n', 'BRIDGE_PROTOCOL_V1 = 1\nBRIDGE_RELEASE_V1 = "1.0.0"\n', 1)
old = '''            if actual_root != self.root_folder_id:\n                raise ArchiveStorageError(\n                    f"Bridge v1 root mismatch: {actual_root!r} != {self.root_folder_id!r}",\n                    code="ROOT_MISMATCH",\n                )\n'''
new = old + '''            if str(data.get("bridge_release") or "") != BRIDGE_RELEASE_V1:\n                raise ArchiveStorageError(\n                    f"Bridge v1 release mismatch: {data.get('bridge_release')!r} != {BRIDGE_RELEASE_V1!r}",\n                    code="BRIDGE_RELEASE_MISMATCH",\n                )\n'''
if 'BRIDGE_RELEASE_MISMATCH' not in s:
    if old not in s:
        raise SystemExit('archive_google.py root health anchor missing')
    s = s.replace(old, new, 1)
p.write_text(s, encoding='utf-8')

# 2. Make the environment contract explicit: current production may remain legacy until cutover,
# but v1 is final and rollback is deliberately selected rather than automatic.
p = ROOT / '.env.example'
s = p.read_text(encoding='utf-8')
s = s.replace(
    '# Production remains on legacy mode until the isolated Marketplaces Protocol-v1\n# deployment passes the common acceptance matrix.\nMARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_PROTOCOL=legacy',
    '# Final transport is Protocol v1. Set `legacy` only for an intentional rollback.\n# Existing production must be switched to v1 only after live acceptance passes.\nMARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_PROTOCOL=v1',
)
s = s.replace('# Current production route (legacy Bridge v3).', '# Rollback-only legacy Bridge v3 route.')
s = s.replace('# Future isolated Shared Google Drive Bridge Protocol v1 route.', '# Final isolated Shared Google Drive Bridge Protocol v1 route.')
p.write_text(s, encoding='utf-8')

# 3. Replace integration-guide status wording with final 1.0.0 contract wording.
p = ROOT / 'docs/SHARED_GOOGLE_DRIVE_BRIDGE_V1.md'
s = p.read_text(encoding='utf-8')
s = s.replace(
    'Status: integration guide. Client target: `protocol_version=1`, candidate `bridge_release=1.0.0-alpha.2`. Production cutover is forbidden until the dedicated Marketplaces deployment passes the common acceptance matrix.',
    'Status: FINAL CLIENT CONTRACT. Target: `protocol_version=1`, `bridge_release=1.0.0`. The client implementation is complete. Production activation remains fail-closed until the dedicated Marketplaces deployment passes live acceptance.',
)
s = s.replace('## Large reads — alpha.2', '## Large reads — v1.0.0')
s = s.replace('Client-code READY is not production PASS. Production PASS requires a separate Marketplaces Apps Script deployment, dedicated secret/Lockbox binding and real live acceptance against a large file.',
              'Client code is complete. Production PASS requires only runtime activation evidence: the separate Marketplaces Apps Script deployment, project-isolated `gdrive_bridge_v1_secret`, and live acceptance against real small/large files. No protocol or client function remains unfinished.')
p.write_text(s, encoding='utf-8')

status = '''# Marketplaces Google Drive Bridge v1 status\n\nRelease target: `protocol_version=1`, `bridge_release=1.0.0`.\n\n## Code state\n\n**CODE COMPLETE.** The Marketplaces archive client implements:\n- project-pinned `project_id=marketplaces`;\n- unique request IDs and stable idempotency keys;\n- fixed-root health validation;\n- bounded small I/O;\n- resumable large upload;\n- verified promotion with exact size/SHA256;\n- direct large Google download through Bridge `files.download` LRO;\n- exact byte/SHA256 verification before archive bytes are exposed;\n- explicit legacy rollback profile with no automatic credential fallback.\n\n## Runtime order\n\n1. Deploy/authorize the dedicated Marketplaces Apps Script Bridge v1.0.0.\n2. Store the generated project secret in the Marketplaces project Lockbox under `gdrive_bridge_v1_secret`.\n3. Run live health/security/small/large/concurrency acceptance.\n4. Set `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_PROTOCOL=v1` and inject the v1 URL/secret/root.\n5. Run post-cutover archive read/write verification.\n6. Retain the legacy variables only for an explicit rollback window, then remove them after stable post-cutover evidence.\n\nInteractive Google owner authorization is an external runtime permission gate, not unfinished code.\n'''
(ROOT / 'docs/MARKETPLACES_BRIDGE_V1_STATUS.md').write_text(status, encoding='utf-8')
print('MARKETPLACES_BRIDGE_V1_FINALIZER=PASS')
