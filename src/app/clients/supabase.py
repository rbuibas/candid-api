# Lazy Supabase client. Constructed on first call so imports and /health
# work without credentials. Uses the service-role key — backend only.

from supabase import Client, create_client

from app.config import get_settings

_client: Client | None = None


def get_supabase() -> Client:
    global _client
    if _client is not None:
        return _client

    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise RuntimeError("Supabase client requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")

    _client = create_client(settings.supabase_url, settings.supabase_service_role_key)
    return _client
