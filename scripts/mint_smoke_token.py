"""Print a short-lived JWT for manual smoke testing (section H of SUPABASE-SETUP.md).

Usage:
    uv run python scripts/mint_smoke_token.py
    uv run python scripts/mint_smoke_token.py --email you@example.com

Requires .env to be populated (SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_JWT_SECRET).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

import jwt  # noqa: E402  (needs dotenv first so pyjwt import sees env)
from supabase import create_client  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", help="Pick a specific user by email (defaults to first user)")
    args = parser.parse_args()

    required = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_JWT_SECRET")
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)}")

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    users = list(sb.auth.admin.list_users())

    if not users:
        sys.exit("No users found — create one via Authentication → Users → Add user first.")

    if args.email:
        user = next((u for u in users if u.email == args.email), None)
        if user is None:
            sys.exit(f"No user with email {args.email!r}. Found: {[u.email for u in users]}")
    else:
        user = users[0]

    now = datetime.now(tz=UTC)
    token = jwt.encode(
        {
            "sub": str(user.id),
            "aud": "authenticated",
            "role": "authenticated",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        os.environ["SUPABASE_JWT_SECRET"],
        algorithm="HS256",
    )

    print(f"User:  {user.email}  ({user.id})")
    print(f"Token: {token}")
    print()
    print(
        f'curl -H "Authorization: Bearer {token}" '
        f"{os.environ['SUPABASE_URL'].replace('supabase.co', 'supabase.co')}"
        " ... "
        "(replace with your API URL)"
    )


if __name__ == "__main__":
    main()
