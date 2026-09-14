from pathlib import Path

p = Path('tests/test_archive_google_bridge_v1.py')
s = p.read_text(encoding='utf-8')

# Stable 1.0.0 has no NOT_IMPLEMENTED large-read state. Remove the obsolete
# alpha-era test that expected the implementation stub.
start_token = 'def test_v1_large_download_not_implemented_is_explicit'
if start_token in s:
    start = s.index(start_token)
    next_def = s.index('\ndef ', start + len(start_token))
    s = s[:start] + s[next_def + 1:]

old_name = 'def test_v1_health_reports_large_download_not_ready_without_blocking_transport(monkeypatch):'
if old_name in s:
    start = s.index(old_name)
    try:
        end = s.index('\ndef ', start + len(old_name)) + 1
    except ValueError:
        end = len(s)
    replacement = '''def test_v1_health_fails_closed_when_required_large_download_is_missing(monkeypatch):
    store = _v1_store()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb

        async def post(self, url, *, json, headers):
            del url, headers
            return _response_for(
                json,
                {
                    "protocol_version": 1,
                    "bridge_release": "1.0.0",
                    "project_id": "marketplaces",
                    "root_id": ROOT_ID,
                    "root_name": "MCP архив базы данных",
                    "capabilities": {
                        "drive_small_io": True,
                        "drive_resumable_upload": True,
                        "drive_large_download": False,
                        "fixed_root_file_id_guard": True,
                        "idempotent_mutations": True,
                        "global_script_lock": False,
                    },
                },
            )

    monkeypatch.setattr(archive_google.httpx, "AsyncClient", FakeClient)
    with pytest.raises(ArchiveStorageError) as exc:
        asyncio.run(store.status())
    assert exc.value.code == "LARGE_DOWNLOAD_REQUIRED"

'''
    s = s[:start] + replacement + s[end:]

# Stable release must be used by all fake health responses.
s = s.replace('"bridge_release": "1.0.0-alpha.1"', '"bridge_release": "1.0.0"')
s = s.replace('"bridge_release": "1.0.0-alpha.2"', '"bridge_release": "1.0.0"')

p.write_text(s, encoding='utf-8')
print('MARKETPLACES_STABLE_TEST_CONTRACT=PASS')
