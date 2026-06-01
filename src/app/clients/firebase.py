# Lazy Firebase Admin app for FCM push. Credentials are loaded from
# FIREBASE_SERVICE_ACCOUNT_B64 (base64-encoded service-account JSON) on first call.

import base64
import json
import logging
from typing import Any, NamedTuple

import firebase_admin
from firebase_admin import credentials, messaging

from app.config import get_settings

logger = logging.getLogger(__name__)

_app: Any = None


class SendResult(NamedTuple):
    success_count: int
    failure_count: int
    # FCM tokens the caller should delete from `devices` — the device has
    # uninstalled the app or the project mapping is wrong.
    invalid_tokens: list[str]


def get_firebase_app() -> Any:
    global _app
    if _app is not None:
        return _app

    settings = get_settings()
    if not settings.firebase_service_account_b64:
        raise RuntimeError("Firebase client requires FIREBASE_SERVICE_ACCOUNT_B64")

    raw = base64.b64decode(settings.firebase_service_account_b64)
    cred = credentials.Certificate(json.loads(raw))
    _app = firebase_admin.initialize_app(cred)
    return _app


def send_push(
    tokens: list[str],
    data: dict[str, Any],
    title: str,
    body: str = "",
) -> SendResult:
    """Multicast a notification + data payload to a batch of FCM tokens.

    FCM data values must be strings; non-string values are stringified here so
    callers can pass ints/UUIDs directly.

    Returns per-batch counts plus the list of tokens that came back as
    unregistered / mismatched — the caller should delete those device rows.
    """
    if not tokens:
        return SendResult(success_count=0, failure_count=0, invalid_tokens=[])

    get_firebase_app()
    msg = messaging.MulticastMessage(
        tokens=tokens,
        notification=messaging.Notification(title=title, body=body),
        data={k: str(v) for k, v in data.items()},
    )
    resp = messaging.send_each_for_multicast(msg)

    invalid: list[str] = []
    for i, r in enumerate(resp.responses):
        exc = getattr(r, "exception", None)
        if exc is not None and isinstance(
            exc,
            messaging.UnregisteredError | messaging.SenderIdMismatchError,
        ):
            invalid.append(tokens[i])

    logger.info(
        "FCM multicast: tokens=%d success=%d failure=%d invalid=%d",
        len(tokens),
        resp.success_count,
        resp.failure_count,
        len(invalid),
    )
    if resp.failure_count:
        for i, r in enumerate(resp.responses):
            exc = getattr(r, "exception", None)
            if exc is not None:
                logger.warning("FCM token[%d] error: %s", i, exc)

    return SendResult(
        success_count=resp.success_count,
        failure_count=resp.failure_count,
        invalid_tokens=invalid,
    )
