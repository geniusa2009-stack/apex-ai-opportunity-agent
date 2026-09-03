"""New-Volunteer Onboarding Checklist -- model + the real, centrally
defined step list.

Step 1 investigation, confirmed directly against the real files:
- `PATCH /admin/users/{user_id}/role` (app/api/v1/endpoints/admin_roles.py)
  is the one real hook point where "this person is now a new volunteer"
  happens -- but until this step, it never recorded WHEN, and there was
  no real per-volunteer onboarding state anywhere in this codebase.
- `User` had no `role_granted_at` (see `app/models/user.py`'s own
  disclosure on why `TimestampMixin.updated_at` is not a substitute) --
  a real, small gap this step closes via migration 0048.
- No real onboarding document exists anywhere in this project's own
  history to link to (checked the community/volunteer-management
  context directly) -- this checklist's step definitions below are a
  new, minimal, honest design grounded in what this specific codebase
  actually has a new Moderator/Senior Moderator to learn, not a
  transcription of some pre-existing document that doesn't exist.

DESIGN: a FIXED, SMALL, server-defined list -- explicitly NOT a
simulated practice environment (that is a separate, later, deliberately
distinct feature on the remaining-items list: "training/practice mode
for new volunteers"). Each step here is a real orientation action a
volunteer takes in the REAL admin panel this codebase already has --
"go look at X real view/capability" -- self-reported completion (the
volunteer marks it done themselves), not auto-detected from page-view
telemetry this codebase has no instrumentation for (auto-detecting
"has this user actually opened the Team tab" would need new tracking
infrastructure this step was never asked to build, and guessing at it
from indirect signals would be exactly the kind of fabricated-signal
shortcut this project's own standing discipline forbids).

`step_key` stays a plain, unconstrained string column on
`OnboardingChecklistProgress` (validated against `ONBOARDING_STEPS`
at the API layer, not a DB-level enum) -- same reasoning
`app/models/notification.py`'s own `Notification.type` docstring
already gives for that column: a closed DB enum is the single most
operationally painful column type in this codebase to extend later,
and this step list is exactly the kind of thing a small team may want
to tweak without an `ALTER TYPE` migration.
"""

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


@dataclass(frozen=True)
class OnboardingStep:
    key: str
    order: int
    # Plain English label -- for API consumers/logs/tests, same posture
    # as every other server-side enum-ish label in this codebase (e.g.
    # `ChatReportReason`'s own values). The REAL bilingual AR/EN copy a
    # human volunteer actually reads lives in the frontend's own label
    # map, matching the established convention every other closed list
    # in `admin-moderation.html` already uses (`REASON_LABEL`,
    # `ACTION_LABEL`, `GUILD_TARGET_TYPE_LABEL`) rather than the server
    # shipping bilingual strings no other endpoint in this codebase does.
    label: str


# The real, fixed, centrally defined checklist -- five real orientation
# steps, each pointing at a real, already-built admin-panel capability a
# brand-new Moderator/Senior Moderator should see at least once before
# they start acting independently. Deliberately NOT elaborate: this is
# orientation/setup tracking, not a graded curriculum.
ONBOARDING_STEPS: tuple[OnboardingStep, ...] = (
    OnboardingStep(
        key="view_team",
        order=1,
        label="Open the Team view and see who else has Moderator/Senior Moderator/Owner access",
    ),
    OnboardingStep(
        key="view_queue",
        order=2,
        label="Open the Review Queue and see how claiming an item works before acting on it",
    ),
    OnboardingStep(
        key="view_activity_summary",
        order=3,
        label="Open your own contribution summary so you know your actions are logged and visible",
    ),
    OnboardingStep(
        key="understand_tier_limits",
        order=4,
        label="Understand what your own tier can and cannot do (e.g. only Senior Moderator+ can permanently delete content)",
    ),
    OnboardingStep(
        key="know_escalation_path",
        order=5,
        label="Know how to escalate a serious case (chat ban, content deletion) to someone with a higher tier if you don't have it yourself",
    ),
    # Training / Practice Mode for New Volunteers addition -- SIXTH real
    # step, deliberately DIFFERENT in kind from the five above: those
    # are self-reported ("the volunteer marks it done themselves," per
    # this module's own docstring, because this codebase has no real
    # signal to verify them by). This one has a REAL, verifiable signal
    # instead (a real `PracticeCaseAttempt` row exists per practice
    # case, see `app/models/practice_case.py`) -- so, unlike the other
    # five, it is SERVER-COMPUTED, not self-reportable. Both
    # `complete_my_onboarding_step`/`uncomplete_my_onboarding_step`
    # (app/api/v1/endpoints/onboarding.py) explicitly reject a direct
    # self-report attempt on this specific key: using a fabricated
    # client-asserted signal when a real one already exists would be a
    # real regression from this codebase's own established honesty
    # discipline, not a shortcut.
    OnboardingStep(
        key="complete_practice_cases",
        order=6,
        label="Complete every case in the training sandbox before acting on real cases",
    ),
)

ONBOARDING_STEP_KEYS: frozenset[str] = frozenset(step.key for step in ONBOARDING_STEPS)


class OnboardingChecklistProgress(Base):
    """One row per COMPLETED step per volunteer -- row presence means
    "this user marked this step done," absence means not-yet-completed.
    Chosen over a full row-per-step-per-user table (with a boolean
    column) because it makes "reset on a fresh grant" (see
    `assign_admin_role`'s own docstring) a single, real, honest
    `DELETE ... WHERE user_id = :id`, and because the real, fixed step
    list this file defines is the actual source of truth for "which
    steps exist" -- a GET endpoint always iterates `ONBOARDING_STEPS`
    and left-joins this table, so an unmarked step is never a missing
    row a client has to distinguish from a broken one.
    """

    __tablename__ = "onboarding_checklist_progress"
    __table_args__ = (
        UniqueConstraint("user_id", "step_key", name="uq_onboarding_progress_user_step"),
        Index("ix_onboarding_progress_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # Validated against `ONBOARDING_STEP_KEYS` at the API layer -- see
    # this module's own docstring for why this stays a plain `String`,
    # not a DB enum.
    step_key: Mapped[str] = mapped_column(String(50), nullable=False)

    completed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<OnboardingChecklistProgress user_id={self.user_id} step_key={self.step_key!r}>"
