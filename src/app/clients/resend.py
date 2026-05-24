# Lazy Resend setup. The `resend` package is module-configured (sets `resend.api_key`),
# so this helper guards configuration behind first-call.

import resend as _resend

from app.config import get_settings

_configured = False


def get_resend():
    global _configured
    if _configured:
        return _resend

    settings = get_settings()
    if not settings.resend_api_key:
        raise RuntimeError("Resend client requires RESEND_API_KEY")

    _resend.api_key = settings.resend_api_key
    _configured = True
    return _resend
