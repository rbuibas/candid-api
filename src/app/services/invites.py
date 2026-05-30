"""Invite-code service.

Codes are 6-character uppercase alphanumeric (e.g. K3J9PQ). The schema's
CHECK allows 6–16, leaving longer codes available later; we lock to 6 for
the bachelor-party MVP.

`secrets.choice` is fine here — we just need an unguessable handful of
characters, not a cryptographic key. The unique constraint on `code` is
the source of truth for collisions; the retry loop handles the (vanishing)
chance of a draw clash.
"""

import secrets
import string

from postgrest.exceptions import APIError
from supabase import Client

_CODE_ALPHABET = string.ascii_uppercase + string.digits
_CODE_LENGTH = 6
_DEFAULT_MAX_RETRIES = 5
_PG_UNIQUE_VIOLATION = "23505"


class InviteCodeGenerationError(Exception):
    """Could not generate a unique invite code within the retry budget."""


def _generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def create_for_group(
    sb: Client,
    group_id: str,
    *,
    max_retries: int = _DEFAULT_MAX_RETRIES,
) -> str:
    """Insert an active invite code for `group_id`. Returns the code string.

    Retries on the (extremely unlikely) collision against the UNIQUE index
    on `invite_codes.code`. 36**6 ≈ 2.2B, so for MVP scale the loop is
    belt-and-braces only.
    """
    for _ in range(max_retries):
        code = _generate_code()
        try:
            sb.table("invite_codes").insert(
                {"group_id": group_id, "code": code, "active": True}
            ).execute()
        except APIError as e:
            if e.code == _PG_UNIQUE_VIOLATION:
                continue
            raise
        return code

    raise InviteCodeGenerationError(
        f"Failed to generate a unique invite code after {max_retries} attempts"
    )
