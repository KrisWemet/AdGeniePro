"""Exercise encrypted network delivery through the real HTTP and money paths."""

import base64
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from sqlalchemy import select

from adgenie.core.metrics import load_performance
from adgenie.models import Click, ClickBankReceipt, Conversion, EntityLevel, MetricSnapshot
from adgenie.networks.clickbank import decrypt_notification

SECRET = "CBTEST1234567890"


def test_decrypts_independent_openssl_vector():
    # Generated with openssl enc -aes-256-cbc using ClickBank's documented key
    # derivation. This avoids testing only our encryption helper against itself.
    envelope = {"iv": "AAECAwQFBgcICQoLDA0ODw==", "notification":
        "AxV4JE1XfhoagBkx8fG7ALvsncgYIwGULO7mhr5l6WgFAphqkC1jr6ZavMiJvbFOnSHQug+q/iOUlPV70NVW0g=="}
    assert decrypt_notification(envelope, SECRET)["transactionType"] == "TEST"


def encrypted(payload, secret=SECRET):
    key = hashlib.sha1(secret.encode()).hexdigest()[:32].encode()
    iv = bytes(range(16))
    padder = PKCS7(128).padder()
    plaintext = json.dumps(payload, ensure_ascii=False).encode()
    padded = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return {"iv": base64.b64encode(iv).decode(),
            "notification": base64.b64encode(enc.update(padded) + enc.finalize()).decode()}


@pytest.fixture
def setup_click(api_client, settings, session):
    settings.clickbank_ins_secret = SECRET
    settings.clickbank_nickname = "testaff"
    offer = api_client.post("/api/offers", json={
        "name": "Water guide", "network": "clickbank", "payout_usd": 40,
        "destination_url": "https://example.hop.clickbank.net/?tid=stale&cbpage=guide",
    }).json()
    launch = api_client.post("/api/campaigns/launch", json={
        "offer_id": offer["id"], "platform": "meta", "daily_budget_usd": 10,
        "angle_count": 1,
    }).json()
    creative_id = launch["creative_ids"][0]
    creative = api_client.get(f"/api/creatives/{creative_id}").json()
    query = urlparse(creative["final_url"]).query
    response = api_client.get("/r?" + query + "&fbclid=real-click-fixture",
                              follow_redirects=False,
                              headers={"User-Agent": "Mozilla/5.0"})
    assert response.status_code == 302
    params = parse_qs(urlparse(response.headers["location"]).query)
    assert "subid" not in params
    assert params["cbpage"] == ["guide"]
    assert re.fullmatch("[a-z0-9_]{32}", params["tid"][0])
    click_id = params["tid"][0]
    # Use yesterday so the normal performance endpoint includes the fixture.
    when = datetime.now(timezone.utc) - timedelta(days=1)
    click = session.scalar(select(Click).where(Click.click_id == click_id))
    click.created_at = when.replace(tzinfo=None) - timedelta(minutes=1)
    session.add(MetricSnapshot(level=EntityLevel.CREATIVE, entity_id=creative_id,
        day=when.date(), impressions=100, clicks=1, spend_micros=10_000_000))
    session.commit()
    return {
        "version": 8.0, "transactionType": "SALE", "receipt": "ABC12345",
        "transactionTime": when.isoformat(), "affiliate": "testaff", "role": "AFFILIATE",
        "totalAccountAmount": "40.25", "totalOrderAmount": "79.00",
        "trackingCodes": [click_id], "attemptCount": 1,
        "customer": {"firstName": "Zoë"},
    }, creative_id


def post(api_client, payload, *, form=False, secret=SECRET):
    envelope = encrypted(payload, secret)
    return api_client.post("/postback/clickbank", **({"data": envelope} if form else {"json": envelope}))


def revenue(session, creative):
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    return load_performance(session, EntityLevel.CREATIVE, creative, yesterday, yesterday)


def test_click_to_encrypted_sale_to_profit_and_refund(api_client, setup_click, session):
    payload, creative = setup_click
    response = post(api_client, payload)
    assert response.status_code == 200
    assert response.json()["matched"] is True
    window = revenue(session, creative)
    assert window.revenue_micros == 40_250_000
    assert window.profit_micros == 30_250_000
    assert window.roas == pytest.approx(4.025)
    # Retries with a different attemptCount are one sale.
    assert post(api_client, {**payload, "attemptCount": 9}, form=True).json()["attribution"] == "duplicate"
    assert revenue(session, creative).conversions == 1
    refund = {**payload, "transactionType": "RFND", "totalAccountAmount": "-40.25"}
    assert post(api_client, refund).json()["status"] == "reversed"
    assert revenue(session, creative).revenue_micros == 0
    assert post(api_client, payload).json()["status"] == "reversed"
    session.expire_all()
    ledger = session.scalar(select(ClickBankReceipt))
    assert "customer" not in json.dumps(ledger.events)


def test_partial_refund_preserves_remaining_commission(api_client, setup_click, session):
    payload, creative = setup_click
    post(api_client, payload)
    refund = {**payload, "transactionType": "RFND", "totalAccountAmount": "-10.25"}
    post(api_client, refund)
    post(api_client, {**refund, "attemptCount": 2})
    assert revenue(session, creative).revenue_micros == 30_000_000


def test_refund_before_sale_cannot_resurrect_refunded_revenue(api_client, setup_click, session):
    payload, creative = setup_click
    refund = {**payload, "transactionType": "RFND", "trackingCodes": [], "totalAccountAmount": "-40.25"}
    assert post(api_client, refund).json()["status"] == "pending"
    assert post(api_client, payload).json()["status"] == "reversed"
    session.expire_all()
    conv = session.scalar(select(Conversion))
    assert conv.creative_id == creative
    assert conv.revenue_micros == 0


def test_rebill_receipt_is_a_separate_payment(api_client, setup_click, session):
    payload, creative = setup_click
    post(api_client, payload)
    bill = {**payload, "receipt": "ABC12345-0001", "transactionType": "BILL", "totalAccountAmount": "12.50"}
    post(api_client, bill)
    post(api_client, bill)
    assert revenue(session, creative).revenue_micros == 52_750_000
    post(api_client, {**bill, "transactionType": "CGBK", "totalAccountAmount": "-12.50"})
    assert revenue(session, creative).revenue_micros == 40_250_000


@pytest.mark.parametrize("kind", ["TEST", "TEST_SALE", "CANCEL-REBILL", "UNCANCEL-REBILL", "ABANDONED_ORDER"])
def test_non_revenue_events_never_create_sales(api_client, setup_click, session, kind):
    payload, _ = setup_click
    response = post(api_client, {**payload, "transactionType": kind})
    assert response.status_code == 200
    assert not response.json()["recorded"]
    assert session.query(Conversion).count() == 0


@pytest.mark.parametrize("changes", [{"role": "VENDOR"}, {"affiliate": "someoneelse"}])
def test_only_configured_affiliate_earnings_are_counted(api_client, setup_click, session, changes):
    payload, _ = setup_click
    assert not post(api_client, {**payload, **changes}).json()["recorded"]
    assert session.query(Conversion).count() == 0


def test_wrong_secret_bad_ciphertext_and_plaintext_are_rejected(api_client, setup_click, session):
    payload, _ = setup_click
    assert post(api_client, payload, secret="WRONGSECRET").status_code == 400
    for body in (payload, {"notification": "!", "iv": "!"}, [1], None):
        assert api_client.post("/postback/clickbank", json=body).status_code == 400
    assert session.query(Conversion).count() == 0


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "bad"])
def test_invalid_sale_money_is_rejected(api_client, setup_click, amount):
    payload, _ = setup_click
    assert post(api_client, {**payload, "totalAccountAmount": amount}).status_code == 400


def test_unmatched_sale_is_recorded_without_crediting_a_creative(api_client, setup_click, session):
    payload, creative = setup_click
    assert post(api_client, {**payload, "trackingCodes": ["unknown"]}).json()["matched"] is False
    assert revenue(session, creative).revenue_micros == 0


def test_v8_external_click_id_is_supported(api_client, setup_click):
    payload, _ = setup_click
    payload["affiliateTrackingParameters"] = {"extclid": payload["trackingCodes"][0]}
    payload["trackingCodes"] = []
    assert post(api_client, payload).json()["matched"] is True


def test_ins_requires_configuration_and_has_a_body_limit(api_client, settings):
    assert api_client.post("/postback/clickbank", json={}).status_code == 503
    settings.clickbank_ins_secret = SECRET
    settings.clickbank_nickname = "testaff"
    assert api_client.post("/postback/clickbank", content=b"x" * 262145).status_code == 413


def test_v7_and_unicode_payloads_decrypt():
    payload = {"version": 7.0, "transactionType": "TEST", "name": "Zoë 水"}
    assert decrypt_notification(encrypted(payload), SECRET) == payload


def test_other_networks_keep_generic_subid(api_client):
    offer = api_client.post("/api/offers", json={"name": "Other", "network": "manual",
        "destination_url": "https://offer.test/", "payout_usd": 20}).json()
    response = api_client.get(f"/r?s=o{offer['id']}", follow_redirects=False)
    assert "subid=" in response.headers["location"]
    assert "?tid=" not in response.headers["location"]
