"""Profile service — DB access for profile read/update.

Router handlers stay thin: they call into here, catch the domain
exceptions, and translate them to HTTP responses.
"""

from uuid import UUID

from supabase import Client

from app.models.profile import Profile, ProfileUpdate


class ProfileNotFoundError(Exception):
    """The authenticated user has no profiles row.

    Should be unreachable in normal flow because the handle_new_user
    trigger creates the row on auth.users insert. Treated as 401 by
    callers (token is valid but the user doesn't exist in our world).
    """


class EmptyPatchError(Exception):
    """Caller sent a PATCH body with no fields set."""


def get_for_user(sb: Client, user_id: UUID) -> Profile:
    result = sb.table("profiles").select("*").eq("id", str(user_id)).maybe_single().execute()
    if not result.data:
        raise ProfileNotFoundError()
    return Profile.model_validate(result.data)


def update_for_user(sb: Client, user_id: UUID, patch: ProfileUpdate) -> Profile:
    update_dict = patch.model_dump(exclude_unset=True)
    if not update_dict:
        raise EmptyPatchError()

    result = sb.table("profiles").update(update_dict).eq("id", str(user_id)).execute()
    if not result.data:
        raise ProfileNotFoundError()
    return Profile.model_validate(result.data[0])
