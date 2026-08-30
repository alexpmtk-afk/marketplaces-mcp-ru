from core.rate_limit import GlobalRateController, redis_connection_kwargs, redis_url_from_env


def _clear(monkeypatch):
    for name in (
        "MARKETPLACE_MCP_REDIS_URL", "MARKETPLACE_MCP_REDIS_HOST",
        "MARKETPLACE_MCP_REDIS_PORT", "MARKETPLACE_MCP_REDIS_PASSWORD",
        "MARKETPLACE_MCP_REDIS_DB", "MARKETPLACE_MCP_REDIS_TLS",
        "MARKETPLACE_MCP_REDIS_CA_CERT", "MARKETPLACE_REQUIRE_REDIS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_component_redis_config_builds_tls_url(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("MARKETPLACE_MCP_REDIS_HOST", "c-test.rw.mdb.yandexcloud.net")
    monkeypatch.setenv("MARKETPLACE_MCP_REDIS_PASSWORD", "a b@c")
    assert redis_url_from_env() == "rediss://:a%20b%40c@c-test.rw.mdb.yandexcloud.net:6380/0"


def test_explicit_url_wins(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("MARKETPLACE_MCP_REDIS_URL", "redis://explicit:6379/2")
    monkeypatch.setenv("MARKETPLACE_MCP_REDIS_HOST", "ignored")
    assert redis_url_from_env() == "redis://explicit:6379/2"


def test_component_config_satisfies_require_redis(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("MARKETPLACE_REQUIRE_REDIS", "1")
    monkeypatch.setenv("MARKETPLACE_MCP_REDIS_HOST", "internal")
    assert GlobalRateController().backend == "redis"


def test_tls_ca_is_forwarded(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("MARKETPLACE_MCP_REDIS_CA_CERT", "/etc/ssl/certs/yandex-cloud-ca.pem")
    assert redis_connection_kwargs("rediss://host:6380/0") == {"ssl_ca_certs": "/etc/ssl/certs/yandex-cloud-ca.pem"}
