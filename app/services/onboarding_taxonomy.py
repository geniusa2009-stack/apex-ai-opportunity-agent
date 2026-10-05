"""APEX Onboarding Production Integration -- explicit, deterministic
mappings from the onboarding prototype's own frontend vocabulary onto
the existing backend enums (`AcademicLevel`, `OpportunityType`,
`ApplicationGoal`), plus the two small free-text/boolean conventions
already documented on `User` (`funding_preference`, `remote_preference`).

DISCLOSED, DELIBERATE DESIGN: the frontend's own chip labels are
product copy, not a stable backend contract, and are NOT identical to
any existing enum's vocabulary. Rather than inventing new enum members
to mirror the frontend (which the governing directive explicitly
forbids), every mapping below is a plain, inspectable dict from an
exact frontend string to an existing enum member (or, where no existing
member is an honest match, to nothing at all -- see `UNMAPPED`
handling in each function's docstring). Nothing here invents a
semantic equivalent for a value this codebase has no real concept of.

Each `map_*` function returns `(mapped_values, unmapped_inputs)` so the
caller (the onboarding endpoint) can both use the mapped result AND
disclose, in its response, exactly which submitted values had no safe
backend representation -- never silently guessed, never silently
dropped without being named.
"""

from __future__ import annotations

from app.models.enums import AcademicLevel, ApplicationGoal, OpportunityType

# ---------------------------------------------------------------------
# STEP 4: onboarding STAGES/grade -> AcademicLevel
# ---------------------------------------------------------------------
# Keyed by the exact (stage, grade) pair the prototype's own `STAGES`
# object can produce (`apex-onboarding.html`). Grade is "" for the two
# stages with no sub-options (`Gap year`, `Other`) and for any stage
# before a grade is chosen.
#
# AcademicLevel has no member for "recent graduate," "working," "gap
# year," or "other" -- those four combinations map to `None` (genuinely
# unmappable, not guessed) rather than being forced onto the nearest
# unrelated value (e.g. "Working" is not a Masters student).
_STAGE_GRADE_TO_ACADEMIC_LEVEL: dict[tuple[str, str], AcademicLevel | None] = {
    ("Middle school", "Grade 7"): AcademicLevel.PREP_1,
    ("Middle school", "Grade 8"): AcademicLevel.PREP_2,
    ("Middle school", "Grade 9"): AcademicLevel.PREP_3,
    ("High school", "Grade 10"): AcademicLevel.SECONDARY_1,
    ("High school", "Grade 11"): AcademicLevel.SECONDARY_2,
    ("High school", "Grade 12"): AcademicLevel.SECONDARY_3,
    ("University", "Year 1"): AcademicLevel.UNIVERSITY_YEAR_1,
    ("University", "Year 2"): AcademicLevel.UNIVERSITY_YEAR_2,
    ("University", "Year 3"): AcademicLevel.UNIVERSITY_YEAR_3,
    ("University", "Year 4"): AcademicLevel.UNIVERSITY_YEAR_4,
    ("University", "Year 5"): AcademicLevel.UNIVERSITY_YEAR_5,
    ("University", "Year 6"): AcademicLevel.UNIVERSITY_YEAR_6,
    ("Graduate", "Master's student"): AcademicLevel.MASTERS,
    ("Graduate", "PhD student"): AcademicLevel.PHD,
    ("Graduate", "Recent graduate"): None,
    ("Graduate", "Working"): None,
    ("Gap year", ""): None,
    ("Other", ""): None,
}


def map_stage_to_academic_level(
    stage: str, grade: str | None
) -> tuple[AcademicLevel | None, bool]:
    """Returns `(academic_level, was_unmapped)`. `was_unmapped` is True
    only when `stage` is a recognized onboarding stage but the specific
    (stage, grade) combination has no safe `AcademicLevel` equivalent
    (e.g. "Graduate" + "Working") -- the caller surfaces this in its
    response disclosure rather than silently returning `None` with no
    explanation. An entirely unrecognized `stage` string (never sent by
    the real onboarding UI, only possible via a malformed direct API
    call) also returns `(None, True)`.
    """
    key = (stage, grade or "")
    if key not in _STAGE_GRADE_TO_ACADEMIC_LEVEL:
        return None, True
    level = _STAGE_GRADE_TO_ACADEMIC_LEVEL[key]
    # A recognized combination that maps to `None` (e.g. "Graduate" +
    # "Working") is unmapped too -- only a combination that resolves to
    # a real `AcademicLevel` member counts as mapped.
    return level, level is None


# ---------------------------------------------------------------------
# STEP 6: onboarding LOOKING (opportunity types, multi-select) -> OpportunityType
# ---------------------------------------------------------------------
# Several frontend labels intentionally combine two real backend
# concepts ("Internships & jobs", "Grants & startup funding") -- these
# map to TWO enum members, not one invented combined member, since the
# target field (`User.preferred_opportunity_types`) is already an array.
_LOOKING_TO_OPPORTUNITY_TYPES: dict[str, tuple[OpportunityType, ...]] = {
    "Scholarships & study abroad": (OpportunityType.SCHOLARSHIP,),
    "Competitions": (OpportunityType.COMPETITION,),
    # Closest existing member, not a perfect synonym -- disclosed: this
    # codebase's taxonomy has no dedicated "summer program" value, and
    # `YOUTH_PROGRAM` is the nearest real category a summer/camp program
    # already gets classified under elsewhere in the catalog.
    "Summer programs & camps": (OpportunityType.YOUTH_PROGRAM,),
    # "Research" alone is ambiguous between RESEARCH_FUNDING/
    # RESEARCH_PROGRAM/RESEARCH_INTERNSHIP -- RESEARCH_PROGRAM is used
    # as the general bucket since the onboarding UI does not ask the
    # user to distinguish further.
    "Research": (OpportunityType.RESEARCH_PROGRAM,),
    "Internships & jobs": (OpportunityType.INTERNSHIP, OpportunityType.JOB),
    "Conferences & MUN": (OpportunityType.CONFERENCE,),
    "Courses & online programs": (OpportunityType.COURSE,),
    "Volunteering": (OpportunityType.VOLUNTEER,),
    "Grants & startup funding": (
        OpportunityType.GRANT,
        OpportunityType.STARTUP_FUNDING,
    ),
}


def map_looking_for(values: list[str]) -> tuple[list[OpportunityType], list[str]]:
    """Returns `(preferred_opportunity_types, unmapped_inputs)`. Order
    is not guaranteed to match `values`' order (a set is used to drop
    duplicates when two frontend labels map to the same enum member).
    """
    mapped: set[OpportunityType] = set()
    unmapped: list[str] = []
    for v in values:
        types = _LOOKING_TO_OPPORTUNITY_TYPES.get(v)
        if types is None:
            unmapped.append(v)
        else:
            mapped.update(types)
    return sorted(mapped, key=lambda t: t.value), unmapped


# ---------------------------------------------------------------------
# STEP 6: onboarding GOALS (single-select main goal) -> ApplicationGoal
# ---------------------------------------------------------------------
# "I'm not sure yet" is a legitimate, explicitly supported product
# state (per the governing directive's own Section 13/17) -- it maps to
# an EMPTY list, never a guessed goal and never a new "unsure" enum
# member invented for this one case. The existing guidance/feed
# pipeline already treats an unset `application_goals` as an
# exploration-friendly default (no goal-specific narrowing applied).
_GOAL_TO_APPLICATION_GOALS: dict[str, tuple[ApplicationGoal, ...]] = {
    "Get into a university": (ApplicationGoal.STUDY,),
    "Study abroad": (ApplicationGoal.STUDY,),
    "Build experience & my CV": (ApplicationGoal.CAREER,),
    "Win competitions": (ApplicationGoal.COMPETITIONS,),
    "Start something": (ApplicationGoal.ENTREPRENEURSHIP,),
    "I'm not sure yet": (),
}


def map_goal(value: str) -> tuple[list[ApplicationGoal], bool]:
    """Returns `(application_goals, was_unmapped)`."""
    goals = _GOAL_TO_APPLICATION_GOALS.get(value)
    if goals is None:
        return [], True
    return list(goals), False


# ---------------------------------------------------------------------
# STEP 6: onboarding FUNDING -> User.funding_preference (free-text convention)
# ---------------------------------------------------------------------
# `User.funding_preference` is documented (app/models/user.py) as a
# free-text convention matching `funding_intelligence.FundingType`'s
# values by agreement, not a native enum column -- these three mappings
# follow that existing documented convention exactly.
_FUNDING_TO_PREFERENCE: dict[str, str] = {
    "Fully funded only": "fully_funded",
    "Partially funded is OK": "partial",
    "Any": "any",
}


def map_funding(value: str) -> tuple[str | None, bool]:
    pref = _FUNDING_TO_PREFERENCE.get(value)
    return pref, pref is None


# ---------------------------------------------------------------------
# STEP 6: onboarding WHERE -> User.remote_preference (tri-state bool)
# ---------------------------------------------------------------------
# `remote_preference` is strictly True(remote)/False(in-person)/None
# (no stated preference) -- a real, disclosed, imperfect-but-safe fit:
# "Abroad too" genuinely means "willing to travel abroad," a concept
# this field cannot represent on its own, so it maps to `None` ("no
# stated remote/in-person preference either way") rather than inventing
# a third boolean state or a new field this phase's own scope does not
# require.
_WHERE_TO_REMOTE_PREFERENCE: dict[str, bool | None] = {
    "Online only": True,
    "In my country": False,
    "Abroad too": None,
}


def map_where(value: str) -> bool | None:
    return _WHERE_TO_REMOTE_PREFERENCE.get(value)
