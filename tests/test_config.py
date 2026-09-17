"""Credential detection and engine configuration.

Both failures here are silent. A half-configured platform that reads as
configured gets a live client instead of the simulator and fails on its first
call, with campaigns already depending on it. A SQLite engine with no busy
timeout serves an error to a visitor whose click was already paid for.
"""

from __future__ import annotations

from adgenie.config import Settings
from adgenie.db import _engine_kwargs


def test_google_without_its_oauth_client_is_not_configured():
    """The refresh token is exchanged for an access token using the client id
    and secret. Counting a deployment as configured without them routes live
    traffic to an adapter that cannot authenticate, instead of to the sandbox
    that would have said so."""
    partial = Settings(
        google_developer_token="dev",
        google_refresh_token="refresh",
        google_customer_id="123",
    )
    assert not partial.has_google

    complete = Settings(
        google_developer_token="dev",
        google_client_id="cid",
        google_client_secret="secret",
        google_refresh_token="refresh",
        google_customer_id="123",
    )
    assert complete.has_google


def test_sqlite_waits_for_a_contended_write_rather_than_failing():
    """Every /r click is a write, and these routes run in a threadpool. Without
    a busy timeout a click landing during an optimizer write raises "database
    is locked" and the visitor never reaches the offer."""
    assert _engine_kwargs("sqlite:///./adgenie.db")["connect_args"]["timeout"] == 30


def test_postgres_recycles_connections_before_a_provider_closes_them():
    """Managed Postgres cuts idle connections at around five minutes. A pool
    that hands out a closed one fails whichever request happened to get it."""
    kwargs = _engine_kwargs("postgresql+psycopg://user@host/db")
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_recycle"] < 300


def test_a_proxied_click_records_the_visitor_not_the_proxy():
    """Every deployment here puts something in front of the API — Caddy in the
    Compose file, the platform edge on a PaaS — and the API publishes no port
    of its own. Reading the socket peer therefore records the proxy on every
    click, so the whole table shares one ip_hash: uniform, plausible-looking
    garbage rather than an error anyone would notice."""
    from adgenie.core.tracking import client_ip

    assert client_ip("10.0.0.7", "203.0.113.9", trust_proxy=True) == "203.0.113.9"
    # Client, then each proxy it passed through. The visitor is leftmost.
    assert client_ip("10.0.0.7", "203.0.113.9, 10.0.0.1", trust_proxy=True) == "203.0.113.9"


def test_the_forwarded_header_is_ignored_unless_a_proxy_is_declared():
    """X-Forwarded-For is a request header. Anywhere the API can be reached
    directly, trusting it lets a caller choose what gets recorded, so it stays
    off until the deployment says a proxy is genuinely in front."""
    from adgenie.core.tracking import client_ip

    assert client_ip("198.51.100.4", "203.0.113.9", trust_proxy=False) == "198.51.100.4"
    # Declared, but the proxy sent nothing usable: fall back to the peer.
    assert client_ip("198.51.100.4", "", trust_proxy=True) == "198.51.100.4"
    assert client_ip("198.51.100.4", " , ", trust_proxy=True) == "198.51.100.4"
    assert client_ip(None, None, trust_proxy=True) is None
