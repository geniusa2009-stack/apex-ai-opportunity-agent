"""Pydantic V2 schemas for the Hyper-Personalized Platform step's three
new models (`app/models/features.py`) -- Certificate, LessonStudyAid,
MentorshipRelation.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enums import AcademicLevel, MentorshipStatus

# `MentorshipRelation.status` is server-computed (set by the router, never
# client-supplied directly) -- same posture as `StudyDuelOut.status`
# (app/schemas/social.py). The only client-supplied action inputs are the
# two `Literal`s below.
_MentorshipRespondAction = Literal["approve", "reject"]


class CertificateOut(BaseModel):
    """A user's issued certificate for a course."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    course_id: UUID
    issued_at: datetime
    certificate_url: str | None


class StudyAidOut(BaseModel):
    """AI-generated flashcards + summary for one `Lesson` or `StudentVideo`.

    Exactly one of `lesson_id`/`student_video_id` is ever populated on a
    real row (see `LessonStudyAid`'s own `CheckConstraint`) -- both are
    still declared here so a single response shape covers either target
    without needing two parallel schemas.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    lesson_id: UUID | None
    student_video_id: UUID | None
    summary_text: str | None
    flashcards: list[dict] | None
    created_at: datetime


class MentorshipUserSummary(BaseModel):
    """A lightweight nested view of the OTHER party in a mentorship
    relation -- same "don't re-serialize the raw edge, show a friendly
    view of the related User" posture as `FriendOut`
    (app/schemas/social.py). `academic_level` is included (unlike
    `FriendOut`) since it's the whole point of a mentor/mentee pairing --
    the `/mentorship` UI needs it to show e.g. "mentor: University Year 3"
    without a second lookup.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str | None
    full_name: str | None
    academic_level: AcademicLevel | None


class MentorshipRelationOut(BaseModel):
    """One mentorship relation, from EITHER party's point of view -- the
    router (see `mentorship.py`) nests whichever of `mentor`/`mentee`
    is the OTHER party relative to `current_user`, so the frontend never
    has to compare `current_user.id` against `mentor_id`/`mentee_id`
    itself to know who's being displayed.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    mentor_id: UUID
    mentee_id: UUID
    status: MentorshipStatus
    requested_at: datetime
    responded_at: datetime | None
    # Populated by the router, not `from_attributes` off the ORM row
    # directly (`MentorshipRelation` itself has no `relationship()` --
    # see that model's own docstring) -- the router fetches the other
    # party's `User` row separately and constructs this field explicitly.
    other_party: MentorshipUserSummary


class MentorshipRequestCreate(BaseModel):
    """POST /mentorship/request request body -- a mentee requesting a
    specific mentor. Per this step's own ask ("Actions to request/approve
    mentors"), the MENTEE always initiates; there is no "mentor offers"
    direction in this pass (disclosed scope boundary -- see router
    docstring)."""

    mentor_id: UUID


class MentorshipRespondRequest(BaseModel):
    """POST /mentorship/{id}/respond request body -- disclosed necessary
    addition beyond the ask's bare "approve" wording: a mentor needs SOME
    way to decline an unwanted request too, or every PENDING row a mentor
    doesn't want becomes permanently stuck (no delete/cancel path named by
    the ask either). `reject` DELETES the row rather than transitioning it
    to a stored `REJECTED` status -- `MentorshipStatus` has no such member
    (see that enum's own docstring on why one wasn't added), so "rejected"
    is modeled as "this relation never existed to begin with," freeing the
    mentee to request a different mentor (or even the same one again)
    afterward, since `uq_mentorship_pair` only blocks a second row while
    one still exists."""

    action: _MentorshipRespondAction


class MentorshipCompleteRequest(BaseModel):
    """POST /mentorship/{id}/complete request body -- disclosed necessary
    addition: `MentorshipStatus.COMPLETED` is a real enum member the
    ask itself names, so some endpoint has to be able to reach it.
    Either party (mentor or mentee) may mark an ACTIVE relation
    COMPLETED -- see the router for the exact authorization check. No
    body fields needed; this is a modeled as a bare `POST`, but a real
    (even if always-empty) request-body schema is kept for symmetry with
    the other two mutation endpoints and to leave room for an optional
    future field (e.g. a closing note) without a breaking wire-format
    change."""

    pass
