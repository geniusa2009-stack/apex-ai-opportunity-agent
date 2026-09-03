"""Two-Person Confirmation for Dangerous Actions.

See `app/models/dangerous_action_confirmation.py`'s own module docstring
for the full Step 1 investigation (the complete, real, four-action list)
and Step 2/3 design (the pending-confirmation state machine, and why the
four legacy routes themselves are now the real "request" step).

STEP 2 DESIGN, ROUTE SHAPE: three real routes here, deliberately
DIFFERENT shapes for DIFFERENT real reasons --

- `POST /admin/moderation/dangerous-actions/{confirmation_id}/approve`
  is resource-id-based, NOT `{item_type}/{item_id}`-shaped like
  `escalate`/`notes`/`claim`. Reasoning: approving is an action on the
  CONFIRMATION itself (a specific pending request someone already made,
  visible on `GET /admin/moderation/dangerous-actions`), not on the
  underlying item directly -- the same item could, in principle, have
  had more than one confirmation raised against it over time (one
  approved, one later re-requested), so the confirmation's own id, not
  the item's, is the real identity a second approver is acting on.
- `POST /admin/moderation/{item_type}/{item_id}/dangerous-actions/
  {action}/override` IS `{item_type}/{item_id}`-shaped, like the other
  item-context actions -- reasoning: an Owner invoking an emergency
  override is acting FROM the context of a specific item they are
  looking at (exactly like raising an escalation or adding a case note),
  not from a confirmation id they may not have (the whole POINT of the
  override is to work even when nobody has requested confirmation yet --
  see `dangerous_action_service.override_confirmation`'s own docstring).
  `{action}` is a real `ModerationAction` path segment, narrowed to the
  four real dangerous actions at the service layer (a fifth/unrelated
  `ModerationAction` value 404s there, same "syntactically valid enum
  member, not in the real dispatch table" posture `admin_claims.py`'s
  own `_CLAIMABLE_MODELS` already established for `item_type`).
- `GET /admin/moderation/dangerous-actions` -- the one real whole-team
  view, same "no per-item context assumed" exception
  `admin_escalations.py::list_active_escalations` already established
  for its own analogous `GET /admin/escalations`.

No path-template ambiguity with `claim`/`escalate`/`notes` (all
`{item_type}/{item_id}/<verb>`): none of THIS file's three routes share
a final literal verb with any of those (`approve`/`override` are new,
and the plain `GET /admin/moderation/dangerous-actions` has a different
real segment count than any `{item_type}/{item_id}/...` route), so
Starlette's per-route pattern match never has to choose between two
otherwise-identical candidates regardless of registration order.
"""

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession, RequireOwner, RequireSeniorModerationAccess, effective_admin_role
from app.api.v1.endpoints.admin_claims import _CLAIMABLE_MODELS
from app.models.dangerous_action_confirmation import DangerousActionConfirmation
from app.models.enums import ModerationAction, ModerationTargetType
from app.models.user import User
from app.schemas.dangerous_actions import DangerousActionConfirmationOut, DangerousActionListOut
from app.services import dangerous_action_service
from app.services.dangerous_action_service import DangerousActionError

router = APIRouter(tags=["admin-dangerous-actions"])


async def _get_item_or_404(db: DbSession, item_type: ModerationTargetType, item_id: uuid.UUID) -> None:
    """Same dispatch-table reuse `admin_escalations.py::_get_item_or_404`/
    `admin_case_notes.py::_get_item_or_404` already established -- a
    dangerous action is only ever taken against the same real
    claimable/reviewable item kinds those features already work from."""
    model = _CLAIMABLE_MODELS.get(item_type)
    if model is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{item_type.value}' is not an item type dangerous actions apply to.",
        )
    item = await db.get(model, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{item_type.value} not found")


async def _get_confirmation_or_404(db: DbSession, confirmation_id: uuid.UUID) -> DangerousActionConfirmation:
    confirmation = await db.get(DangerousActionConfirmation, confirmation_id)
    if confirmation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Confirmation not found")
    return confirmation


def _to_out(confirmation: DangerousActionConfirmation, requested_by_name: str | None, executed_by_name: str | None) -> DangerousActionConfirmationOut:
    return DangerousActionConfirmationOut(
        id=confirmation.id,
        action=confirmation.action,
        item_type=confirmation.item_type,
        item_id=confirmation.item_id,
        notes=confirmation.notes,
        status=confirmation.status,
        is_override=confirmation.is_override,
        requested_by=confirmation.requested_by,
        requested_by_name=requested_by_name,
        requested_at=confirmation.requested_at,
        executed_by=confirmation.executed_by,
        executed_by_name=executed_by_name,
        executed_at=confirmation.executed_at,
    )


@router.post(
    "/admin/moderation/dangerous-actions/{confirmation_id}/approve",
    response_model=DangerousActionConfirmationOut,
)
async def approve_dangerous_action(
    confirmation_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireSeniorModerationAccess,
) -> DangerousActionConfirmationOut:
    """Senior-Moderator-or-higher -- the real second approver. REJECTS
    SELF-APPROVAL with a real 403 (`dangerous_action_service.
    approve_confirmation`'s own required central rule: the requester
    cannot also be the approver). Executes the real underlying action
    (video delete/purge, chat message delete, guild content delete)
    immediately on success -- this IS the moment the irreversible action
    actually happens.
    """
    confirmation = await _get_confirmation_or_404(db, confirmation_id)

    try:
        confirmation = await dangerous_action_service.approve_confirmation(
            db, confirmation=confirmation, approver=current_user, approver_role=effective_admin_role(current_user)
        )
    except DangerousActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return _to_out(confirmation, None, current_user.full_name)


@router.post(
    "/admin/moderation/{item_type}/{item_id}/dangerous-actions/{action}/override",
    response_model=DangerousActionConfirmationOut,
)
async def override_dangerous_action(
    item_type: ModerationTargetType,
    item_id: uuid.UUID,
    action: ModerationAction,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireOwner,
) -> DangerousActionConfirmationOut:
    """OWNER-ONLY. The real, deliberate emergency-override path -- see
    `app/models/dangerous_action_confirmation.py`'s own `is_override`
    docstring for the full design reasoning behind why this is a
    SEPARATE endpoint (never a silent flag on `approve`). Executes the
    real underlying action immediately, with NO second approver required
    -- exists specifically for the real concern this task's own prompt
    raised: a hard two-person requirement could otherwise block the
    Owner from acting when no second real person is available. Always
    real-logged as `DANGEROUS_ACTION_OVERRIDDEN`, never
    `DANGEROUS_ACTION_APPROVED` -- the audit trail always shows this was
    an override, not a normal confirmation.
    """
    await _get_item_or_404(db, item_type, item_id)

    try:
        confirmation = await dangerous_action_service.override_confirmation(
            db,
            action=action,
            item_type=item_type,
            item_id=item_id,
            notes=None,
            owner=current_user,
            owner_role=effective_admin_role(current_user),
        )
    except DangerousActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return _to_out(confirmation, None, current_user.full_name)


@router.get("/admin/moderation/dangerous-actions", response_model=DangerousActionListOut)
async def list_pending_dangerous_actions(
    db: DbSession,
    current_user: CurrentUser,
    _: RequireSeniorModerationAccess,
    limit: int = 50,
    offset: int = 0,
) -> DangerousActionListOut:
    """Senior-Moderator-or-higher -- every currently PENDING dangerous
    action across every item type, oldest first (same "longest-waiting
    surfaces first" posture `admin_escalations.py::
    list_active_escalations` already established). This is the real
    queue a second approver works from -- and the same view that lets
    the ORIGINAL requester confirm their own request is still waiting,
    not silently lost.
    """
    rows, total = await dangerous_action_service.list_pending(db, limit=limit, offset=offset)
    items = [_to_out(confirmation, requested_by_name, None) for confirmation, requested_by_name in rows]
    return DangerousActionListOut(items=items, total=total)
