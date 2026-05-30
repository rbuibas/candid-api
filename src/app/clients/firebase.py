# Lazy Firebase Admin app for FCM push. Credentials are loaded from
# FIREBASE_SERVICE_ACCOUNT_B64 (base64-encoded service-account JSON) on first call.

import base64
import json
from typing import Any

import firebase_admin
from firebase_admin import credentials

from app.config import get_settings

_app: Any = None


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
