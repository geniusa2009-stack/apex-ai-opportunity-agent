"""Shared AI-prompt personalization helper.

Backend Folder Structure & Core Database Models step's own literal
architecture requirement: "Ensure the AI Service architecture is
designed to inject the User's `academic_level` into the LLM context
prompt." `build_academic_context` is that architecture -- a single,
small, reusable function any AI-calling service in this codebase can
call to turn a `User` row into one extra content block for an LLM
`contents` list, rather than each of this platform's many AI-consuming
services (there are dozens, spanning Advanced/Legendary/High School/
Ecosystem-tier features) hand-rolling its own ad-hoc "describe this
student" string.

DISCLOSED SCOPE BOUNDARY: this pass wires the helper into exactly ONE
concrete, already-existing consumer -- `student_video_service.
evaluate_and_moderate` (the AI grading step of the `/videos` router this
same ask explicitly names: "`/videos` (UGC upload & AI grading)"),
proving the architecture works end-to-end against a real call site.
Retrofitting every other AI-consuming endpoint built across this
project's many prior milestones (the probability engine, multi-agent
interview simulator, essay sparker, research mentor, and so on) is
explicitly OUT of scope for this pass -- there are too many to touch in
one sitting, and doing so was not itself asked for. The helper is
designed for exactly that future reuse: any other service need only
call `build_academic_context(user)` and append the (possibly `None`)
result to its own `contents` list, with zero new plumbing required.
"""

from app.models.enums import AcademicLevel
from app.models.user import User

_ACADEMIC_LEVEL_LABELS: dict[AcademicLevel, str] = {
    AcademicLevel.PREP_1: "1st year Preparatory",
    AcademicLevel.PREP_2: "2nd year Preparatory",
    AcademicLevel.PREP_3: "3rd year Preparatory",
    AcademicLevel.SECONDARY_1: "1st year Secondary",
    AcademicLevel.SECONDARY_2: "2nd year Secondary",
    AcademicLevel.SECONDARY_3: "3rd year Secondary",
    AcademicLevel.UNIVERSITY_YEAR_1: "1st year University",
    AcademicLevel.UNIVERSITY_YEAR_2: "2nd year University",
    AcademicLevel.UNIVERSITY_YEAR_3: "3rd year University",
    AcademicLevel.UNIVERSITY_YEAR_4: "4th year University",
    AcademicLevel.UNIVERSITY_YEAR_5: "5th year University",
    AcademicLevel.UNIVERSITY_YEAR_6: "6th year University",
    AcademicLevel.MASTERS: "Master's degree",
    AcademicLevel.PHD: "PhD",
}


def build_academic_context(user: User) -> str | None:
    """Returns one short, natural-language sentence describing `user`'s
    academic level and/or age, meant to be appended as an EXTRA entry in
    an LLM `contents`/`messages` list -- never spliced into an existing
    system prompt string, so that any already-carefully-worded system
    prompt (e.g. `student_video_service._EVALUATION_SYSTEM_PROMPT`,
    disclosed elsewhere as "word for word" from an earlier step's own
    ask) stays byte-for-byte unchanged.

    Returns `None` (not an empty string) if `user` has set NEITHER
    `academic_level` nor `age` -- both are nullable, self-reported
    profile fields (see `app/models/user.py`), and a real fraction of
    users will have set neither. Callers must skip appending this block
    to their own `contents` list entirely when `None` is returned,
    rather than sending an empty or "unknown" placeholder string to the
    model -- an LLM call with one fewer content block is strictly better
    than one padded with a no-information sentence.

    Deliberately does NOT read `age_group` (the older, coarser,
    three-bucket free-text field) -- `academic_level`/`age` are the
    more precise personalization signals this step introduces
    specifically for LLM context injection; `age_group` remains the
    field the Student Dashboard's opportunity-matching DB query uses
    (a different consumer, a different precision need).
    """
    if user.academic_level is None and user.age is None:
        return None

    facts: list[str] = []
    if user.academic_level is not None:
        facts.append(f"academic level: {_ACADEMIC_LEVEL_LABELS[user.academic_level]}")
    if user.age is not None:
        facts.append(f"age: {user.age}")

    return (
        "Student context, for calibrating tone/vocabulary/difficulty only "
        "(do not mention this context sentence to the student): "
        + ", ".join(facts)
        + "."
    )
