"""HTTP surface: contracts, guard rails and the public tracking endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from adgenie.models import ConversionStatus


@pytest.fixture
def offer_payload() -> dict:
    return {
        "name": "CalmLeaf Sleep Support",
        "destination_url": "https://offer.test/calmleaf",
        "network": "clickbank",
        "vertical": "supplements",
        "payout_usd": 40.0,
        "expected_reversal_rate": 0.1,
        "product_description": "A magnesium and L-theanine blend taken before bed.",
        "key_benefits": ["wind down without grogginess", "keep a routine"],
        "proof_points": ["Third-party tested in a US facility"],
        "is_regulated": True,
    }


@pytest.fixture
def created_offer(api_client, offer_payload) -> dict:
    return api_client.post("/api/offers", json=offer_payload).json()


# --- system ----------------------------------------------------------------


def test_health_reports_configuration_honestly(api_client):
    body = api_client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["dry_run"] is False
    assert body["copywriter"] == "template"
    assert body["platforms"]["meta"]["simulated"] is True
    # Health has to report the setting, not a hardcoded hope. An operator who
    # turned auditing off needs to see that from here.
    assert body["landing_page_audit"] == "disabled"


def test_dashboard_is_served(api_client):
    response = api_client.get("/")
    assert response.status_code == 200
    assert "AdGenie Pro" in response.text


def test_openapi_documents_every_router(api_client):
    paths = api_client.get("/openapi.json").json()["paths"]
    for path in ("/api/offers", "/api/campaigns/launch", "/api/optimizer/run", "/postback"):
        assert path in paths


# --- offers ----------------------------------------------------------------


def test_create_and_read_an_offer(api_client, offer_payload):
    created = api_client.post("/api/offers", json=offer_payload)
    assert created.status_code == 201
    body = created.json()
    assert body["payout_usd"] == 40.0
    # Expected value is net of the reversal rate, which is what the optimizer uses.
    assert body["expected_value_usd"] == pytest.approx(36.0)

    fetched = api_client.get(f"/api/offers/{body['id']}").json()
    assert fetched["name"] == offer_payload["name"]
    assert api_client.get("/api/offers").json()[0]["id"] == body["id"]


def test_offer_requires_an_http_url(api_client, offer_payload):
    offer_payload["destination_url"] = "javascript:alert(1)"
    assert api_client.post("/api/offers", json=offer_payload).status_code == 422


def test_revshare_offer_needs_its_economics(api_client, offer_payload):
    offer_payload.update({"payout_type": "cps", "payout_usd": 0.0})
    response = api_client.post("/api/offers", json=offer_payload)
    assert response.status_code == 422
    assert "average_order_value" in response.json()["detail"]


def test_missing_offer_returns_404(api_client):
    assert api_client.get("/api/offers/9999").status_code == 404


# --- copy ------------------------------------------------------------------


def test_generate_copy_returns_reviewed_variants(api_client, created_offer):
    response = api_client.post(
        "/api/copy/generate",
        json={"offer_id": created_offer["id"], "platform": "google", "variants": 3},
    )
    assert response.status_code == 200
    variants = response.json()
    assert len(variants) == 3
    assert len({v["angle"] for v in variants}) == 3
    for variant in variants:
        assert variant["compliance"]["verdict"] != "block"
        assert all(len(h) <= 30 for h in variant["headlines"])


def test_generate_copy_for_a_named_angle(api_client, created_offer):
    variants = api_client.post(
        "/api/copy/generate",
        json={
            "offer_id": created_offer["id"],
            "platform": "meta",
            "angle": "social_proof",
        },
    ).json()
    assert len(variants) == 1
    assert variants[0]["angle"] == "social_proof"


def test_review_endpoint_blocks_policy_violations(api_client, created_offer):
    body = api_client.post(
        "/api/copy/review",
        json={
            "platform": "meta",
            "offer_id": created_offer["id"],
            "headlines": ["Are you diabetic?"],
            "primary_texts": ["Lose 30 pounds guaranteed!"],
        },
    ).json()
    assert body["verdict"] == "block"
    codes = {f["code"] for f in body["findings"]}
    assert {"PERSONAL_ATTRIBUTE_DIRECT", "SPECIFIC_WEIGHT_LOSS"} <= codes
    assert all(f["policy_ref"] for f in body["findings"])


def test_review_endpoint_passes_clean_copy(api_client):
    body = api_client.post(
        "/api/copy/review",
        json={
            "platform": "google",
            "headlines": ["A Simpler Routine", "Third Party Tested", "See The Details"],
            "descriptions": [
                "A calm evening routine you can keep. Paid link.",
                "Ships in two days with free returns.",
            ],
        },
    ).json()
    assert body["verdict"] == "pass"


def test_angles_are_listed(api_client):
    angles = api_client.get("/api/angles").json()
    assert len(angles) >= 8
    assert all(a["key"] and a["thesis"] for a in angles)


# --- campaigns -------------------------------------------------------------


def test_launch_creates_a_paused_campaign_by_default(api_client, created_offer):
    result = api_client.post(
        "/api/campaigns/launch",
        json={
            "offer_id": created_offer["id"],
            "platform": "meta",
            "daily_budget_usd": 45.0,
            "angle_count": 2,
        },
    ).json()
    assert result["launched"] == 2
    assert not result["errors"]

    detail = api_client.get(f"/api/campaigns/{result['campaign_id']}").json()
    assert detail["campaign"]["status"] == "paused"
    assert len(detail["ad_groups"]) == 2
    assert detail["ad_groups"][0]["creatives"][0]["final_url"].startswith("https://")


def test_launch_refuses_a_budget_over_the_global_cap(api_client, created_offer, settings):
    response = api_client.post(
        "/api/campaigns/launch",
        json={
            "offer_id": created_offer["id"],
            "platform": "meta",
            "daily_budget_usd": settings.global_daily_budget_cap_usd + 1,
        },
    )
    assert response.status_code == 422
    assert "global cap" in response.json()["detail"]


def test_launch_rejects_a_nonpositive_budget(api_client, created_offer):
    response = api_client.post(
        "/api/campaigns/launch",
        json={"offer_id": created_offer["id"], "platform": "meta", "daily_budget_usd": 0},
    )
    assert response.status_code == 422


def test_launch_unknown_offer_is_404(api_client):
    response = api_client.post(
        "/api/campaigns/launch",
        json={"offer_id": 4242, "platform": "meta", "daily_budget_usd": 10.0},
    )
    assert response.status_code == 404


def test_campaign_status_toggle_cascades(api_client, created_offer):
    launched = api_client.post(
        "/api/campaigns/launch",
        json={
            "offer_id": created_offer["id"],
            "platform": "meta",
            "daily_budget_usd": 30.0,
            "angle_count": 1,
        },
    ).json()
    campaign_id = launched["campaign_id"]

    api_client.post(f"/api/campaigns/{campaign_id}/status?active=true")
    detail = api_client.get(f"/api/campaigns/{campaign_id}").json()
    assert detail["campaign"]["status"] == "active"
    assert detail["ad_groups"][0]["status"] == "active"
    assert detail["ad_groups"][0]["creatives"][0]["status"] == "active"

    api_client.post(f"/api/campaigns/{campaign_id}/status?active=false")
    detail = api_client.get(f"/api/campaigns/{campaign_id}").json()
    assert detail["ad_groups"][0]["creatives"][0]["status"] == "paused"


def test_creative_detail_exposes_the_compliance_report(api_client, created_offer):
    launched = api_client.post(
        "/api/campaigns/launch",
        json={
            "offer_id": created_offer["id"], "platform": "meta",
            "daily_budget_usd": 20.0, "angle_count": 1,
        },
    ).json()
    creative_id = launched["creative_ids"][0]
    body = api_client.get(f"/api/creatives/{creative_id}").json()
    assert "compliance_report" in body
    assert body["compliance_report"]["verdict"] in ("pass", "warn")


# --- optimizer -------------------------------------------------------------


def test_optimizer_run_is_recorded_even_with_nothing_to_do(api_client):
    result = api_client.post("/api/optimizer/run", json={}).json()
    assert "run_id" in result
    assert result["proposed"] == 0
    runs = api_client.get("/api/optimizer/runs").json()
    assert runs[0]["run_id"] == result["run_id"]


def test_sync_rejects_a_reversed_date_range(api_client):
    response = api_client.post(
        "/api/optimizer/sync", json={"since": "2026-03-10", "until": "2026-03-01"}
    )
    assert response.status_code == 422


def test_approving_a_missing_action_is_404(api_client):
    assert api_client.post("/api/optimizer/actions/999/approve").status_code == 404


def test_rejecting_an_action_marks_it_rejected(api_client, created_offer, session):
    from adgenie.models import ActionType, EntityLevel, OptimizationAction

    action = OptimizationAction(
        level=EntityLevel.CREATIVE,
        entity_id=1,
        action=ActionType.PAUSE,
        rule="test",
        reason="test",
    )
    session.add(action)
    session.commit()

    body = api_client.post(
        f"/api/optimizer/actions/{action.id}/reject?reason=not+now"
    ).json()
    assert body["status"] == "rejected"


def test_audit_log_is_exposed(api_client, created_offer):
    api_client.post(
        "/api/campaigns/launch",
        json={
            "offer_id": created_offer["id"], "platform": "meta",
            "daily_budget_usd": 20.0, "angle_count": 1,
        },
    )
    entries = api_client.get("/api/audit").json()
    assert any(e["operation"] == "create_campaign" for e in entries)


def test_rebalance_unknown_ad_group_is_404(api_client):
    assert api_client.get("/api/optimizer/rebalance/999").status_code == 404


# --- tracking --------------------------------------------------------------


def _launch(api_client, offer_id):
    return api_client.post(
        "/api/campaigns/launch",
        json={
            "offer_id": offer_id, "platform": "meta",
            "daily_budget_usd": 20.0, "angle_count": 1,
        },
    ).json()


def test_click_redirects_to_the_offer_with_a_subid(api_client, created_offer):
    launched = _launch(api_client, created_offer["id"])
    creative = api_client.get(f"/api/creatives/{launched['creative_ids'][0]}").json()
    subid = creative["final_url"].split("s=")[1].split("&")[0]

    response = api_client.get(f"/r?s={subid}&fbclid=IwAR9", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://offer.test/calmleaf")
    assert "subid=" in location
    # The platform click id is forwarded so offline conversion upload can work.
    assert "fbclid=IwAR9" in location


def test_click_on_an_unknown_offer_is_404(api_client):
    assert api_client.get("/r?s=o9999", follow_redirects=False).status_code == 404


def test_postback_requires_the_shared_secret(api_client, created_offer):
    response = api_client.post(
        "/postback",
        json={"transaction_id": "t1", "revenue": 40.0},
    )
    assert response.status_code == 401


def test_postback_attributes_revenue_to_the_creative(api_client, created_offer, settings):
    launched = _launch(api_client, created_offer["id"])
    creative = api_client.get(f"/api/creatives/{launched['creative_ids'][0]}").json()
    subid = creative["final_url"].split("s=")[1].split("&")[0]

    redirect = api_client.get(f"/r?s={subid}", follow_redirects=False)
    click_id = redirect.headers["location"].split("subid=")[1].split("&")[0]

    body = api_client.post(
        "/postback",
        json={
            "click_id": click_id,
            "transaction_id": "txn-42",
            "network": "clickbank",
            "revenue": 40.0,
            "status": "approved",
        },
        headers={"X-Postback-Secret": settings.postback_secret},
    ).json()

    assert body["attribution"] == "click_id"
    assert body["creative_id"] == creative["id"]
    assert body["status"] == "approved"


def test_postback_get_form_is_accepted(api_client, created_offer, settings):
    response = api_client.get(
        f"/postback?transaction_id=t-get&revenue=40&secret={settings.postback_secret}"
    )
    assert response.status_code == 200
    assert response.json()["attribution"] == "unmatched"


def test_postback_rejects_an_unknown_status(api_client, settings):
    response = api_client.get(
        f"/postback?transaction_id=t&status=weird&secret={settings.postback_secret}"
    )
    assert response.status_code == 422


def test_duplicate_postback_is_idempotent(api_client, created_offer, settings):
    payload = {
        "transaction_id": "txn-dupe",
        "network": "clickbank",
        "revenue": 40.0,
        "status": "approved",
    }
    headers = {"X-Postback-Secret": settings.postback_secret}
    first = api_client.post("/postback", json=payload, headers=headers).json()
    second = api_client.post("/postback", json=payload, headers=headers).json()
    assert first["conversion_id"] == second["conversion_id"]


def test_performance_endpoint_returns_intervals(api_client, created_offer):
    _launch(api_client, created_offer["id"])
    rows = api_client.get("/api/performance?level=creative&days=7").json()
    # No delivery has been simulated, so the list is empty rather than fabricated.
    assert rows == []
