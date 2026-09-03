"""Training / Practice Mode for New Volunteers -- endpoints.

Three real endpoints, same "Moderator+, own record only" shape
`/me/onboarding*` already established:
    GET    /me/practice                 (Moderator+, own record only)
    POST   /me/practice/{case_key}/act  (Moderator+, own record only)
    DELETE /me/practice/{case_key}/act  (Moderator+, own record only --
                                          real undo for a case a
                                          volunteer wants to re-rehearse
                                          from a clean state)

Routing-precedence check (same discipline as every other router in this
codebase): `/me/practice*` shares no path segment position with any
other already-registered route (no other router owns a literal
`me/practice` prefix) -- registration order is provably irrelevant.

See `app/services/practice_case_service.py`'s own module docstring for
the real airtight-boundary guarantee this router's own handlers below
rely on -- neither handler imports, or could import, any real
mutation function.
"""

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession, RequireModerationAccess, effective_admin_role
from app.models.practice_case import PRACTICE_ACTIONS_BY_CASE_TYPE, PRACTICE_CASE_KEYS, PRACTICE_CASES
from app.schemas.practice_case import (
    PracticeActionRequest,
    PracticeActionResponse,
    PracticeCaseOut,
    PracticeChecklistOut,
)
from app.services import practice_case_service
from app.services.practice_case_service import PracticeCaseError

router = APIRouter(tags=["practice-cases"])


def _to_out(case, attempt) -> PracticeCaseOut:
    return PracticeCaseOut(
        key=case.key,
        case_type=case.case_type,
        order=case.order,
        title=case.title,
        fake_actor_name=case.fake_actor_name,
        scenario=case.scenario,
        content_text=case.content_text,
        allowed_actions=list(PRACTICE_ACTIONS_BY_CASE_TYPE[case.case_type]),
        attempted=attempt is not None,
        action_taken=attempt.action_taken if attempt else None,
        notes=attempt.notes if attempt else None,
        acted_at=attempt.acted_at if attempt else None,
    )


@router.get("/me/practice", response_model=PracticeChecklistOut)
async def get_my_practice_cases(
    current_user: CurrentUser,
    db: DbSession,
    _: RequireModerationAccess,
) -> PracticeChecklistOut:
    """Moderator-or-higher. Always the caller's OWN record -- same
    no-parameter-means-your-own shape `GET /me/onboarding` already
    established. Real cases from the fixed `PRACTICE_CASES` bank,
    merged with this volunteer's own real attempts, if any -- never
    scored (Scope Decision 1, app/models/practice_case.py)."""
    attempts = await practice_case_service.load_attempts(db, user_id=current_user.id)
    items = [_to_out(case, attempts.get(case.key)) for case in PRACTICE_CASES]
    attempted_count = len(attempts)
    return PracticeChecklistOut(
        items=items,
        total=len(PRACTICE_CASES),
        attempted_count=attempted_count,
        all_attempted=PRACTICE_CASE_KEYS.issubset(attempts.keys()),
    )


@router.post("/me/practice/{case_key}/act", response_model=PracticeActionResponse)
async def act_on_practice_case(
    case_key: str,
    payload: PracticeActionRequest,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireModerationAccess,
) -> PracticeActionResponse:
    """Moderator-or-higher, own record only. Real input validation (a
    real 404 for an unknown case, a real 422 for an action that isn't
    real for this case type, a real 403 mirroring the actual tier
    limit -- see `practice_case_service.validate_action`'s own
    docstring) -- but NEVER a graded right/wrong outcome: every valid
    action is recorded identically. Structurally incapable of any real
    side effect -- see `practice_case_service`'s own module docstring."""
    try:
        practice_case_service.validate_action(
            case_key=case_key, action=payload.action, actor_role=effective_admin_role(current_user)
        )
    except PracticeCaseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    await practice_case_service.record_attempt(
        db, user_id=current_user.id, case_key=case_key, action=payload.action, notes=payload.notes
    )

    attempts = await practice_case_service.load_attempts(db, user_id=current_user.id)
    return PracticeActionResponse(
        success=True,
        case_key=case_key,
        action_taken=payload.action,
        all_attempted=PRACTICE_CASE_KEYS.issubset(attempts.keys()),
    )


@router.delete("/me/practice/{case_key}/act", response_model=PracticeActionResponse)
async def reset_my_practice_case(
    case_key: str,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireModerationAccess,
) -> PracticeActionResponse:
    """Moderator-or-higher, own record only. Real undo, same reversible-
    toggle posture `DELETE /me/onboarding/{step_key}/complete` already
    established -- idempotent (un-attempting an already-unattempted
    case is a harmless no-op, not a 404)."""
    if case_key not in PRACTICE_CASE_KEYS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown practice case")

    await practice_case_service.reset_attempt(db, user_id=current_user.id, case_key=case_key)

    attempts = await practice_case_service.load_attempts(db, user_id=current_user.id)
    return PracticeActionResponse(
        success=True,
        case_key=case_key,
        action_taken="",
        all_attempted=PRACTICE_CASE_KEYS.issubset(attempts.keys()),
    )
