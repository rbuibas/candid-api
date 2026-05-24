# Lazy Firebase Admin app for FCM push. Credentials JSON is loaded from env on first call.

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
    if not settings.firebase_credentials_json:
        raise RuntimeError("Firebase client requires FIREBASE_CREDENTIALS_JSON")

    cred = credentials.Certificate(json.loads(settings.firebase_credentials_json))
    _app = firebase_admin.initialize_app(cred)
    return _app
