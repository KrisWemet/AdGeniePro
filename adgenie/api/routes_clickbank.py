"""Public ClickBank encrypted INS receiver, separate from generic postbacks."""

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import config
from ..db import get_session
from ..networks.clickbank import configured, decrypt_notification, receive_notification

router = APIRouter(tags=["clickbank"])
MAX_NOTIFICATION_BYTES = 256 * 1024


@router.post("/postback/clickbank")
async def clickbank_ins(request: Request, session: Session = Depends(get_session)) -> dict:
    settings = config.get_settings()
    if not configured(settings):
        raise HTTPException(503, "Configure CLICKBANK_INS_SECRET and CLICKBANK_NICKNAME")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_NOTIFICATION_BYTES:
            raise HTTPException(413, "notification too large")
    try:
        content_type = request.headers.get("content-type", "").split(";")[0].lower()
        if content_type == "application/x-www-form-urlencoded":
            from urllib.parse import parse_qs
            fields = parse_qs(body.decode("utf-8"), strict_parsing=True)
            envelope = {k: v[0] for k, v in fields.items() if len(v) == 1}
        else:
            envelope = json.loads(body)
        payload = decrypt_notification(envelope, settings.clickbank_ins_secret)
        result = receive_notification(session, payload, settings)
        session.commit()
        return result
    except (ValueError, TypeError, KeyError):
        session.rollback()
        raise HTTPException(400, "invalid ClickBank notification") from None
