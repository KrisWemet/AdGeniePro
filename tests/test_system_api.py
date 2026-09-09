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
