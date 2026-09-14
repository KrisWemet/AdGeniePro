def test_preflight_endpoint_is_available_for_deployed_diagnostics(api_client):
    response = api_client.get("/api/preflight?platform=meta")

    assert response.status_code == 200
    body = response.json()
    assert body["live_requested"] is False
    assert "configuration_ready" in body
    assert "ready_for_live_test" in body
    assert body["checks"]


def test_preflight_endpoint_rejects_unknown_platform(api_client):
    response = api_client.get("/api/preflight?platform=tiktok")
    assert response.status_code == 422


def test_offer_preflight_requires_existing_identifier_shape(api_client):
    assert api_client.get("/api/preflight/offer").status_code == 422
    assert api_client.get("/api/preflight/offer?offer_id=0").status_code == 422


def test_offer_preflight_uses_deployed_session(api_client, monkeypatch):
    from adgenie.api import routes_system
    calls = []
    def check(session, settings, platform, offer_id):
        calls.append((session, platform.value, offer_id))
        return {"ready_to_spend": False, "checks": []}
    monkeypatch.setattr(routes_system, "run_offer_preflight", check)
    response = api_client.get("/api/preflight/offer?offer_id=1&platform=meta")
    assert response.status_code == 200
    assert response.json()["ready_to_spend"] is False
    assert calls[0][1:] == ("meta", 1)
