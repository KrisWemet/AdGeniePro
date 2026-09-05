"""Shared fixtures. Every test runs against an isolated in-memory database."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from adgenie import db as db_module
from adgenie.config import Settings
from adgenie.db import Base
from adgenie.models import Offer, PayoutType, Platform
from adgenie.money import usd_to_micros
from adgenie.platforms.sandbox import SandboxPlatform


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite://",
        dry_run=False,
        public_base_url="https://track.test",
        postback_secret="test-secret",
        secret_key="test-key",
        anthropic_api_key=None,
        global_daily_budget_cap_usd=500.0,
        # Test offers point at domains that do not resolve, and these tests are
        # about launch mechanics rather than destinations. The landing-page
        # tests exercise the auditor directly with a mock transport.
        audit_landing_pages=False,
    )


@pytest.fixture
def engine():
    # StaticPool keeps one connection so an in-memory database survives across
    # sessions within a test.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture
def session(engine):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    yield session
    session.close()


@pytest.fixture
def api_client(engine, settings, monkeypatch):
    """A TestClient wired to the isolated test database."""
    from fastapi.testclient import TestClient

    from adgenie.main import app

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_session():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", factory)

    import adgenie.config as config_module

    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    for module in (
        "adgenie.main",
        "adgenie.api.routes_offers",
        "adgenie.api.routes_campaigns",
        "adgenie.api.routes_optimizer",
        "adgenie.api.routes_tracking",
        "adgenie.api.routes_funnel",
        "adgenie.core.tracking",
        "adgenie.api.security",
    ):
        import importlib

        mod = importlib.import_module(module)
        if hasattr(mod, "get_settings"):
            monkeypatch.setattr(mod, "get_settings", lambda: settings)

    app.dependency_overrides[db_module.get_session] = override_session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def offer(session) -> Offer:
    offer = Offer(
        name="CalmLeaf Sleep Support",
        network="clickbank",
        vertical="supplements",
        destination_url="https://offer.test/calmleaf",
        payout_type=PayoutType.CPA,
        payout_micros=usd_to_micros(40.00),
        expected_reversal_rate=0.0,
        product_description="A magnesium and L-theanine blend taken before bed.",
        target_audience="Adults 30-55 with irregular schedules",
        key_benefits=["wind down without grogginess", "keep a consistent routine"],
        proof_points=["Third-party tested in a US facility"],
        geo_targets=["US"],
        is_regulated=True,
    )
    session.add(offer)
    session.commit()
    return offer


@pytest.fixture
def sandbox_meta() -> SandboxPlatform:
    return SandboxPlatform(Platform.META, seed=99)


@pytest.fixture
def sandbox_google() -> SandboxPlatform:
    return SandboxPlatform(Platform.GOOGLE, seed=99)


@pytest.fixture
def rng() -> random.Random:
    return random.Random(4242)


@pytest.fixture
def week() -> tuple[date, date]:
    until = date(2026, 3, 10)
    return until - timedelta(days=6), until
