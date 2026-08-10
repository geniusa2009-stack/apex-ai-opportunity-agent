from dataclasses import dataclass
from uuid import UUID

import jwt
from passlib.context import CryptContext

from app.core.config import settings

# `deprecated="auto"` means if we ever add a second scheme (e.g. migrating
# hash algorithms later), passlib will auto-upgrade old hashes on next
# successful verify rather than requiring a one-off migration script.
#
# hash_password/verify_password are no longer called anywhere in this
# codebase as of the Supabase integration step (Supabase is now the sole
# authentication authority -- this backend only verifies tokens Supabase
# already issued, it never issues its own or checks a password itself).
# Left in place as generic utilities rather than deleted: they cost
# nothing to keep, and something like a future admin/superuser bootstrap
# script would plausibly want exactly this. `User.hashed_password` (the
# column these used to populate) is now nullable for the same reason --
# see app/models/user.py.
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """Note: bcrypt only uses the first 72 bytes of the input -- anything
    beyond that is silently ignored, not an error, and not something this
    function tries to work around. Fine for realistic passwords; worth
    knowing if a caller ever accepts extremely long input unchecked."""
    return _pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return _pwd_context.verify(plain_password, hashed_password)


class TokenPayloadError(Exception):
    """Raised for any invalid JWT: expired, bad signature, bad audience,
    malformed, or a `sub` claim that isn't a UUID. Callers
    (app/api/deps.py) turn this into a 401 -- it's never meant to
    surface as a 500."""


@dataclass(frozen=True)
class SupabaseTokenPayload:
    """The two claims app/api/deps.py actually needs out of a verified
    Supabase access token. Bundled into one dataclass rather than two
    separate decode calls so there's exactly one place that verifies the
    signature -- see the docstring on `decode_supabase_access_token`."""

    user_id: UUID
    # `email` is best-effort: Supabase includes it on standard email/
    # password and OAuth sign-ins, but a token minted for a
    # phone-only or anonymous-auth user may not have one. `None` here
    # just means "don't have an email to seed the local profile with
    # yet," not an error.
    email: str | None


def decode_supabase_access_token(token: str) -> SupabaseTokenPayload:
    """Decode and verify a Supabase-issued access token, returning its
    `sub` (user id) and `email` claims.

    Verification happens entirely locally against `SUPABASE_JWT_SECRET`
    -- no network call to Supabase is made per-request, matching the
    ask's explicit "verify... locally without hitting the network"
    requirement. Supabase signs access tokens with HS256 and always
    includes `"aud": "authenticated"` -- `audience="authenticated"` is
    passed here so PyJWT actually validates that claim rather than
    silently ignoring it (PyJWT rejects a token that has an `aud` claim
    if you don't tell it what audience to expect, so omitting this
    wasn't an option). If a Supabase project is ever configured with a
    non-default audience, this string would need to change to match.
    """
    try:
        payload = jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience="authenticated",
        )
    except jwt.PyJWTError as exc:
        raise TokenPayloadError(str(exc)) from exc

    subject = payload.get("sub")
    if subject is None:
        raise TokenPayloadError("Token missing `sub` claim")

    try:
        user_id = UUID(subject)
    except ValueError as exc:
        raise TokenPayloadError(f"Token `sub` is not a valid UUID: {subject!r}") from exc

    email = payload.get("email")
    return SupabaseTokenPayload(user_id=user_id, email=email if isinstance(email, str) else None)
