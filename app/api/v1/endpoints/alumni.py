import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.alumni import Alumnus
from app.models.opportunity import Opportunity
from app.models.user import User
from app.services.alumni_matchmaker import (
    NoAlumniCandidatesError,
    alumni_matchmaker_gateway,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/match-alumni", tags=["alumni"])


class MatchAlumniRequest(BaseModel):
    opportunity_id: UUID


class MatchedAlumnusOut(BaseModel):
    id: UUID
    full_name: str
    university: str | None
    major: str | None
    country: str | None
    current_position: str | None
    linkedin_url: str | None
    email: str | None


class MatchAlumniResponse(BaseModel):
    opportunity_id: UUID
    matched_alumnus: MatchedAlumnusOut
    matching_reason: str
    icebreaker_message: str
    networking_tips: list[str]


def _build_student_profile(user: User) -> dict:
    return {
        "full_name": user.full_name,
        "major": user.major,
        "degree_level": user.degree_level,
        "graduation_year": user.graduation_year,
        "skills": ", ".join(user.skills) if user.skills else None,
    }


def _build_alumni_profile(alumnus: Alumnus) -> dict:
    return {
        "id": str(alumnus.id),
        "full_name": alumnus.full_name,
        "university": alumnus.university,
        "major": alumnus.major,
        "country": alumnus.country,
        "degree_level": alumnus.degree_level,
        "current_position": alumnus.current_position,
        "bio": alumnus.bio,
    }


# Path is exactly "/match-alumni" -- same `@router.post("")` +
# `prefix="/match-alumni"` pattern used by every other single-purpose
# POST endpoint since Feature 1.
@router.post("", response_model=MatchAlumniResponse)
async def match_alumni(
    payload: MatchAlumniRequest, current_user: CurrentUser, db: DbSession
) -> MatchAlumniResponse:
    opportunity = await db.get(Opportunity, payload.opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")

    # Exact match on this opportunity only -- not a fuzzy "similar
    # opportunities" search (see the `matched_opportunity_id` note in
    # app/models/alumni.py and the README's "Not included" section for
    # this step).
    result = await db.execute(
        select(Alumnus).where(Alumnus.matched_opportunity_id == payload.opportunity_id)
    )
    alumni_rows = list(result.scalars().all())
    if not alumni_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No alumni are currently on record for this opportunity.",
        )

    alumni_by_id = {str(a.id): a for a in alumni_rows}

    student_profile = _build_student_profile(current_user)
    alumni_profiles = [_build_alumni_profile(a) for a in alumni_rows]
    opportunity_details = {
        "title": opportunity.title,
        "description": opportunity.description,
        "funding_details": opportunity.funding_details,
        "country": opportunity.country,
    }

    try:
        match_result = await alumni_matchmaker_gateway.match_alumni_and_draft(
            student_profile, alumni_profiles, opportunity_details
        )
    except NoAlumniCandidatesError:
        # Shouldn't happen given the empty-list check above, but keeps
        # the service's own precondition guard meaningful if this router
        # is ever called with a pre-fetched list some other way.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No alumni are currently on record for this opportunity.",
        )
    except Exception:
        logger.exception(
            "Alumni matchmaking failed for user %s, opportunity %s",
            current_user.id,
            opportunity.id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Alumni matchmaking is temporarily unavailable.",
        )

    matched = alumni_by_id.get(match_result.best_match_alumnus_id)
    if matched is None:
        # The model returned an id that wasn't in the candidate set we
        # gave it -- a hallucination, not a valid match. Failing loudly
        # here (502) is safer than silently returning an icebreaker
        # message with no real matched alumnus / contact info attached,
        # which the student could otherwise mistake for a genuine match.
        logger.error(
            "Alumni matchmaker returned unknown alumnus id %r (candidates: %s) "
            "for user %s, opportunity %s",
            match_result.best_match_alumnus_id,
            list(alumni_by_id.keys()),
            current_user.id,
            opportunity.id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Alumni matchmaking returned an invalid match. Please try again.",
        )

    return MatchAlumniResponse(
        opportunity_id=opportunity.id,
        matched_alumnus=MatchedAlumnusOut(
            id=matched.id,
            full_name=matched.full_name,
            university=matched.university,
            major=matched.major,
            country=matched.country,
            current_position=matched.current_position,
            linkedin_url=matched.linkedin_url,
            email=matched.email,
        ),
        matching_reason=match_result.matching_reason,
        icebreaker_message=match_result.icebreaker_message,
        networking_tips=match_result.networking_tips,
    )
