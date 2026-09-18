from adgenie.config import Settings, production_config_problems


def _prod(**overrides) -> Settings:
    values = dict(
        environment="prod",
        api_key="operator-key",
        secret_key="a-real-secret-value",
        cors_origins=["https://track.example.com"],
        public_base_url="https://track.example.com",
        database_url="postgresql+psycopg2://adgenie:pw@db:5432/adgenie",
    )
    values.update(overrides)
    return Settings(**values)


def test_a_correct_production_config_starts():
    assert production_config_problems(_prod()) == []


def test_production_refuses_to_start_with_open_admin_routes():
    """/api launches campaigns and moves budgets; unauthenticated it is a
    public spend button."""
    problems = production_config_problems(_prod(api_key=None))
    assert any("API_KEY" in p for p in problems)


def test_production_refuses_sqlite_because_records_vanish_with_the_container():
    problems = production_config_problems(_prod(database_url="sqlite:///./adgenie.db"))
    assert any("SQLite" in p for p in problems)


def test_production_refuses_wildcard_cors_example_secret_and_plain_http():
    problems = production_config_problems(
        _prod(cors_origins=["*"], secret_key="dev-insecure-change-me", public_base_url="http://x.test")
    )
    assert len(problems) == 3


def test_development_is_never_blocked():
    assert production_config_problems(Settings(environment="dev")) == []


def test_healthz_reports_a_reachable_database_without_auth(api_client, settings):
    settings.api_key = "operator-key"
    response = api_client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
