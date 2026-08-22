from collections.abc import Callable, Coroutine
from typing import Annotated

from fastapi import Depends, HTTPException, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import SupabaseTokenPayload, TokenPayloadError, decode_supabase_access_token
from app.db.session import get_db
from app.models.user import User
from app.services import coach_service

# Supabase integration step: swapped from `OAuth2PasswordBearer(tokenUrl=
# ".../auth/login")` to plain `HTTPBearer`. `OAuth2PasswordBearer` exists
# to point Swagger's "Authorize" button at a token-issuing endpoint this
# backend owns -- but that endpoint (`POST /auth/login`) was removed as
# part of this same step, since Supabase is now the sole token issuer.
# `HTTPBearer` instead gives Swagger a plain "paste your bearer token"
# box, which is the correct shape for a backend that only ever verifies
# tokens someone else issued.
_bearer_scheme = HTTPBearer(auto_error=True)

_CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)

# Reused as a plain, no-default annotation (no `= Depends(...)`) everywhere
# below and in the endpoint modules. This matters: Python requires
# non-default parameters to come before defaulted ones in a function
# signature, and `x: Foo = Depends(...)` *is* a defaulted parameter
# syntactically. Mixing that style with a bare `x: CurrentUser` in the
# wrong order is a real `SyntaxError`, not just a style nit -- using the
# `Annotated[...]` form for every dependency here sidesteps the ordering
# question entirely.
DbSession = Annotated[AsyncSession, Depends(get_db)]


async def _resolve_or_provision_user(token_payload: SupabaseTokenPayload, db: AsyncSession) -> User:
    """Shared core of "turn a verified Supabase token payload into a
    local `User` row," extracted (English-Only Gamified Real-time Chat
    step) so BOTH `get_current_user` (below, plain HTTP requests) and
    `get_websocket_user` (below, WebSocket upgrade requests) go through
    the exact same auto-provisioning logic rather than risking two
    copies of this security-relevant behavior drifting apart. Nothing
    about this function's own body changed from what `get_current_user`
    already did before this step -- it was simply lifted out unchanged.
    """
    user = await db.get(User, token_payload.user_id)

    if user is None:
        # Auto-provisioning, per explicit approval: the FIRST time a
        # request arrives with a valid Supabase JWT for a `sub` UUID we
        # haven't seen before, create the local profile row on the spot
        # rather than rejecting with a 401. `id` is set to the token's
        # `sub` (not the column's `default=uuid.uuid4` -- that default
        # is effectively dead now, kept only as a harmless fallback for
        # any code path that ever constructs a `User` without an
        # explicit id). `email` is seeded from the token's `email` claim
        # when present; `hashed_password` is left `None` (the column is
        # now nullable -- see app/models/user.py) since Supabase never
        # gives this backend a password to store. `is_active` keeps its
        # model default of `True`.
        #
        # Trade-off worth being explicit about: this means any user who
        # can obtain a valid Supabase-signed token automatically gets a
        # row in this table, with no separate local approval step. That
        # was the explicitly chosen design (over requiring a dedicated
        # sync/provisioning endpoint before first use) -- flagging it
        # here in case a future step wants an allow-list, invite gating,
        # or similar in front of this.
        user = User(id=token_payload.user_id, email=token_payload.email)
        db.add(user)
        await db.commit()
        await db.refresh(user)

    return user


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer_scheme)],
    db: DbSession,
) -> User:
    try:
        token_payload = decode_supabase_access_token(credentials.credentials)
    except TokenPayloadError:
        raise _CREDENTIALS_EXCEPTION

    user = await _resolve_or_provision_user(token_payload, db)

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]

# Separate `HTTPBearer(auto_error=False)` instance -- NOT the same
# `_bearer_scheme` object `get_current_user` uses above, which has
# `auto_error=True` and raises its own 401 the moment the header is
# missing/malformed, before this dependency ever runs. An endpoint that
# wants to stay usable by BOTH signed-out and signed-in callers (Real
# Age/Academic-Year/Gender Opportunity Filtering step's own explicit
# "additive -- don't break the public unauthenticated listing"
# requirement) needs a variant that treats "no credentials at all" as a
# valid, non-error case, not a rejection.
_bearer_scheme_optional = HTTPBearer(auto_error=False)


async def get_current_user_optional(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme_optional)],
    db: DbSession,
) -> User | None:
    """Same resolution as `get_current_user` when a valid bearer token IS
    present; returns `None` (never raises) for every case that would
    otherwise be a 401/403 -- no `Authorization` header at all, a
    malformed/expired/invalid token, or an inactive user. An endpoint
    using this dependency must treat `None` as "behave exactly like the
    fully public, unauthenticated case," not as an error condition --
    the whole point of this dependency is to make personalization
    ADDITIVE on top of an endpoint that already works with no identity
    at all, per this step's own explicit requirement.
    """
    if credentials is None:
        return None
    try:
        token_payload = decode_supabase_access_token(credentials.credentials)
    except TokenPayloadError:
        return None

    user = await _resolve_or_provision_user(token_payload, db)
    if not user.is_active:
        return None
    return user


CurrentUserOptional = Annotated[User | None, Depends(get_current_user_optional)]


# --- English-Only Gamified Real-time Chat ---
#
# `get_websocket_user` is NOT built as a FastAPI `Depends()` the way
# `get_current_user` is, and deliberately does not reuse `HTTPBearer`.
# Two real constraints force this, not style preference:
#
# 1. The browser-native `WebSocket` API (what any real frontend uses to
#    open `ws://`/`wss://` connections) has NO way to set custom request
#    headers on the handshake -- `Authorization: Bearer <token>` simply
#    cannot be attached the way an ordinary `fetch()` call attaches it.
#    This is a real Web Platform limitation, not a gap in this backend.
# 2. Even if it could, this project's own established auth pattern
#    (`apex-frontend/lib/serverAuth.ts`) stores the access token in an
#    httpOnly cookie specifically so browser JavaScript can NEVER read
#    it -- which also means client-side WebSocket-opening code has no
#    token value available to attach even as a query parameter, without
#    a deliberate, disclosed exception. See `apex-frontend/app/api/chat/
#    ws-ticket/route.ts` for how the frontend bridges this: a same-origin
#    Route Handler reads the httpOnly cookie SERVER-SIDE and hands the
#    token back to the browser for this one purpose.
#
# Given that, the token is accepted as a `?token=` query string parameter
# on the WebSocket URL instead -- the standard, widely-used workaround
# for "browsers can't set WS headers." `websocket.query_params` (not
# `websocket.headers`) is where FastAPI/Starlette exposes it. Returns
# `None` (never raises) on ANY failure -- expired/invalid/missing token,
# inactive user -- so the caller (`app/api/v1/endpoints/chat.py`'s
# WebSocket handler) can `.accept()` the connection first and then close
# it with a specific application close code + human-readable reason,
# which is how this step's own ask wants an auth failure surfaced (a
# real close code, not a silently-refused TCP handshake the browser's
# WebSocket API can't easily distinguish from a network error).
async def get_websocket_user(websocket: WebSocket, db: AsyncSession) -> User | None:
    token = websocket.query_params.get("token")
    if not token:
        return None

    try:
        token_payload = decode_supabase_access_token(token)
    except TokenPayloadError:
        return None

    user = await _resolve_or_provision_user(token_payload, db)
    if not user.is_active:
        return None
    return user


# --- Ecosystem Feature 4: AI Accountability Coach & Premium Token Economy ---
#
# `require_tokens` lives here (rather than in app/api/v1/endpoints/coach.py)
# for the same reason `CurrentUser`/`DbSession` do: it's cross-cutting
# request-dependency infrastructure any router can attach to any endpoint,
# not something specific to the coach feature's own routes. Putting it in
# coach.py would have meant other endpoint modules (essay_humanizer.py,
# multi_agent_interview.py) importing FROM another router module --
# nothing in this codebase does that today, and this dependency has no
# actual reason to live there instead of alongside the other shared
# dependencies.
#
# Deducts tokens BEFORE the wrapped endpoint's own body runs, per the
# ask's literal instruction ("Before calling the Gemini API... deduct
# them"). This means a downstream failure in the endpoint's own logic
# (e.g. the Gemini call itself failing after tokens were already taken)
# is NOT something this dependency can fix on its own -- FastAPI
# dependencies don't get to see whether the route handler that ran after
# them succeeded or failed. Each gated endpoint is responsible for
# calling `coach_service.refund_tokens` itself if its own paid-for work
# fails; see app/api/v1/endpoints/essay_humanizer.py and
# app/api/v1/endpoints/multi_agent_interview.py for that usage.
def require_tokens(feature_key: str, cost: int) -> Callable[..., Coroutine[None, None, None]]:
    async def _dependency(current_user: CurrentUser, db: DbSession) -> None:
        try:
            await coach_service.charge_for_feature(db, current_user.id, feature_key, cost)
        except coach_service.InsufficientTokensError:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Earn more tokens by completing tasks! This feature costs {cost} tokens.",
            )

    return _dependency


# --- Core Video Media Processing Pipeline & Intelligent Recommendation
# Engine (built on top of Ecosystem Features 13 & 14) ---
#
# `require_admin` is a small, disclosed refactor: `app/api/v1/endpoints/
# courses.py` originally defined its own private `_require_admin(current_user)`
# helper (Ecosystem Features 13 & 14's disclosed content-authoring gate)
# and called it directly at the top of each admin route. This step adds
# a second router (`media_router.py`) that needs the EXACT same check
# for its own admin-only video-upload endpoint -- duplicating a
# security-relevant check across two files is a real bug risk (the two
# copies could silently drift), so it moves here instead, alongside
# every other cross-cutting request dependency, and both routers now
# import and depend on this one copy.
async def require_admin(current_user: CurrentUser) -> None:
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only administrators can perform this action."
        )


RequireAdmin = Annotated[None, Depends(require_admin)]
