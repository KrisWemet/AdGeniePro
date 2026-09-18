"""ClickBank INS v7/v8 decoding and receipt reconciliation.

Protocol: https://support.clickbank.com/en/articles/10535147
The AES key is the first 32 ASCII characters of the SHA-1 hex digest, NOT
the binary digest. This is ClickBank's protocol, not a new encryption design.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..core.tracking import record_conversion, secret_is_placeholder
from ..models import Click, ClickBankReceipt, Conversion, ConversionStatus
from ..money import usd_to_micros

FINANCIAL_TYPES = {"SALE", "BILL", "RFND", "CGBK", "INSF"}
REVERSALS = {"RFND", "CGBK", "INSF"}


def configured(settings: Settings) -> bool:
    secret = settings.clickbank_ins_secret or ""
    return bool(
        re.fullmatch(r"[A-Z0-9]{1,16}", secret)
        and not secret_is_placeholder(secret)
        and settings.clickbank_nickname
    )


def decrypt_notification(envelope: dict, secret: str) -> dict:
    """Reject undecodable payloads with one generic error; never log plaintext."""
    try:
        iv = base64.b64decode(envelope["iv"], validate=True)
        ciphertext = base64.b64decode(envelope["notification"], validate=True)
        if len(iv) != 16 or not ciphertext or len(ciphertext) % 16:
            raise ValueError
        key = hashlib.sha1(secret.encode("utf-8")).hexdigest()[:32].encode("ascii")
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        # ClickBank's examples trim bytes 0..32; covers PKCS7 and historical
        # whitespace padding without stripping escaped Unicode inside JSON.
        plaintext = plaintext.rstrip(bytes(range(33)))
        payload = json.loads(plaintext.decode("utf-8"), parse_float=Decimal)
        if not isinstance(payload, dict) or str(payload.get("version")) not in {"7", "7.0", "8", "8.0"}:
            raise ValueError
        if not isinstance(payload.get("transactionType"), str):
            raise ValueError
        return payload
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("invalid ClickBank notification") from exc


def _amount(payload: dict, key: str, *, required: bool = True) -> int:
    try:
        value = Decimal(str(payload[key] if required else payload.get(key, 0)))
        if not value.is_finite() or abs(value) > Decimal("1000000000"):
            raise ValueError
        return usd_to_micros(value)
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise ValueError("invalid ClickBank amount") from exc


def _time(value: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError
        return timestamp.astimezone(timezone.utc).replace(tzinfo=None)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("invalid ClickBank transaction time") from exc


def _click_id(session: Session, payload: dict) -> str | None:
    candidates = payload.get("trackingCodes") or []
    if not isinstance(candidates, list):
        candidates = [candidates]
    fields = payload.get("affiliateTrackingParameters") or {}
    if isinstance(fields, dict):
        candidates = [fields.get("extclid"), fields.get("tid"), *candidates]
    candidates = list(dict.fromkeys(
        c for c in candidates if isinstance(c, str) and 0 < len(c) <= 100
    ))
    if not candidates:
        return None
    # Each id is looked up as received and lowercased, as a precaution against
    # a network upper-casing one in transit: an id that matches no click is a
    # sale credited to no creative. Both forms are literal values, so the
    # unique index on click_id is still used — wrapping the column in lower()
    # would turn this per-sale lookup into a scan of every click ever served.
    #
    # This does not fold the stored side. Ids issued before 2026-09-13 are
    # mixed case, and one of those returned in a different case than it was
    # issued still will not match.
    lookup = list(dict.fromkeys([*candidates, *(c.lower() for c in candidates)]))
    known = list(session.scalars(
        select(Click.click_id).where(Click.click_id.in_(lookup))
    ))
    if len(known) > 1:
        raise ValueError("ambiguous ClickBank tracking identifiers")
    return known[0] if known else candidates[0]


def receive_notification(session: Session, payload: dict, settings: Settings) -> dict:
    kind = payload["transactionType"]
    if kind == "TEST" or kind.startswith("TEST_"):
        return {"accepted": True, "test": True, "recorded": False}
    if payload.get("role") != "AFFILIATE" or str(payload.get("affiliate", "")).lower() != (settings.clickbank_nickname or "").lower():
        return {"accepted": True, "recorded": False, "reason": "not_configured_affiliate"}
    if kind not in FINANCIAL_TYPES:
        return {"accepted": True, "recorded": False, "reason": "non_financial_event"}

    receipt = payload.get("receipt", "")
    if not isinstance(receipt, str) or not re.fullmatch(r"[A-Za-z0-9-]{4,40}", receipt):
        raise ValueError("invalid ClickBank receipt")
    when = _time(payload.get("transactionTime"))
    earned = _amount(payload, "totalAccountAmount")
    sale_amount = abs(_amount(payload, "totalOrderAmount", required=False))
    if kind not in REVERSALS and earned < 0:
        raise ValueError("negative commission on ClickBank sale")
    click_id = _click_id(session, payload)
    # Rebill receipt suffixes are preserved. Never collapse them to the parent
    # subscription receipt or later payments would overwrite the initial sale.
    ledger_id = f"{settings.clickbank_nickname.lower()}:{receipt}"
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise ValueError("ClickBank ledger requires SQLite or PostgreSQL")
    # Upsert plus a row lock serializes concurrent deliveries on PostgreSQL;
    # SQLite serializes writers at the INSERT. Both updates commit atomically.
    session.execute(insert(ClickBankReceipt).values(receipt=ledger_id, events={}).on_conflict_do_nothing())
    ledger = session.scalar(select(ClickBankReceipt).where(
        ClickBankReceipt.receipt == ledger_id
    ).with_for_update().execution_options(populate_existing=True))
    event = {
        "type": kind, "time": when.isoformat(), "earned": earned,
        "sale_amount": sale_amount, "click_id": click_id,
    }
    # One sale per full receipt. Reversal retries exclude attemptCount and
    # tracking fields, which can change without another financial transaction.
    identity = [kind, receipt]
    if kind in REVERSALS:
        identity += [when.isoformat(), abs(earned), sale_amount,
                     sorted(str(i.get("itemNo", "")) for i in payload.get("lineItems", []) if isinstance(i, dict))]
    event_key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    events = dict(ledger.events)
    duplicate = event_key in events
    if not duplicate:
        events[event_key] = event
        ledger.events = events
    credits = [e for e in events.values() if e["type"] not in REVERSALS]
    debits = [e for e in events.values() if e["type"] in REVERSALS]
    gross = sum(e["earned"] for e in credits)
    net = max(0, gross - sum(abs(e["earned"]) for e in debits))
    # A refund that arrives first waits for the sale. Late sale retries cannot
    # restore refunded revenue; partial refunds retain the remaining earnings.
    status = ConversionStatus.PENDING if not credits else (
        ConversionStatus.REVERSED if debits and not net else ConversionStatus.APPROVED
    )
    source = min(credits, key=lambda e: e["time"]) if credits else event
    chosen_click = next((e["click_id"] for e in credits if e["click_id"]), click_id)
    existing = session.scalar(select(Conversion).where(
        Conversion.network == "clickbank", Conversion.network_txn_id == ledger_id
    ))
    if existing is None:
        existing, method = record_conversion(
            session, network="clickbank", network_txn_id=ledger_id,
            click_id=chosen_click, revenue_micros=net,
            sale_amount_micros=sum(e["sale_amount"] for e in credits),
            status=status, occurred_at=_time(source["time"] + "+00:00"),
            event_name="rebill" if source["type"] == "BILL" else "sale",
            raw={"receipt": receipt, "ins_version": str(payload["version"])},
        )
    else:
        method = "duplicate" if duplicate else "updated"
        # Fill attribution when the sale follows an untracked refund. Do not
        # reassign a known conversion to an unrelated later tracking code.
        if existing.offer_id is None and chosen_click and credits:
            from ..core.tracking import attribution_window_ok
            click = session.scalar(select(Click).where(Click.click_id == chosen_click))
            if click and attribution_window_ok(click, _time(source["time"] + "+00:00")):
                for field in ("click_id", "offer_id", "campaign_id", "ad_group_id", "creative_id"):
                    setattr(existing, field, getattr(click, field))
        existing.status = status
        existing.revenue_micros = net
        existing.sale_amount_micros = sum(e["sale_amount"] for e in credits)
        existing.occurred_at = _time(source["time"] + "+00:00")
    session.flush()
    return {
        "accepted": True, "recorded": True, "conversion_id": existing.id,
        "attribution": method, "matched": existing.offer_id is not None,
        "status": existing.status.value, "revenue_micros": net,
    }
