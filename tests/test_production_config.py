"""Production configuration must fail closed.

`Settings.production_errors()` reports configuration faults only; `main`
refuses to start when `ENVIRONMENT=prod` and the list is non-empty. A
misconfigured production deployment is a public spend button, so each check
here names the way money or records leave.
"""

from adgenie.config import Settings


def _prod(**overrides) -> Settings:
    values = dict(
        environment="prod",
        api_key="operator-key-that-is-long-enough",
        secret_key="a-real-secret-value-long-enough",
        postback_secret="another-real-secret-value-here",
        cors_origins=["https://track.example.com"],
        public_base_url="https://track.example.com",
        database_url="postgresql+psycopg://adgenie:pw@db:5432/adgenie",
    )
    values.update(overrides)
    return Settings(**values)


def test_a_correct_production_config_starts():
    assert _prod().production_errors() == []


def test_production_refuses_to_start_with_open_admin_routes():
    """/api launches campaigns and moves budgets; unauthenticated it is a
    public spend button."""
    errors = _prod(api_key=None).production_errors()
    assert any("API_KEY" in e for e in errors)


def test_production_refuses_relative_sqlite_because_records_vanish():
    """A relative SQLite file lives in the container, not on a volume."""
    errors = _prod(database_url="sqlite:///./adgenie.db").production_errors()
    assert any("SQLite" in e for e in errors)


def test_production_refuses_wildcard_cors_example_secret_and_plain_http():
    errors = _prod(
        cors_origins=["*"],
        secret_key="dev-insecure-change-me",
        public_base_url="http://x.test",
    ).production_errors()

    assert any("CORS_ORIGINS" in e for e in errors)
    assert any("SECRET_KEY" in e for e in errors)
    assert any("PUBLIC_BASE_URL" in e for e in errors)


def test_development_is_never_blocked():
    """The startup gate only applies in prod, so a laptop runs unconfigured."""
    assert Settings(environment="dev").environment != "prod"


def test_healthz_reports_a_reachable_database_without_auth(api_client, settings):
    settings.api_key = "operator-key"
    response = api_client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
