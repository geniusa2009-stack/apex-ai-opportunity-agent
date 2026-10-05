"""APEX Onboarding Production Integration -- the ONE canonical minimum-
age rule for the whole product.

DISCLOSED: before this step, APEX had no server-side age floor anywhere.
The only existing age-range check in the repository
(`app.services.summer_program_matchmaker.MIN_AGE`/`MAX_AGE`, 12-19) is a
domain-specific eligibility filter for summer-program matching, not an
account-level minimum age, and is intentionally left untouched here --
this module does not change it, replace it, or get referenced by it.

The onboarding prototype's own client-side "under 13" check
(`apex-onboarding.html`'s `blocked()`) is UX only -- it can be bypassed
by calling the API directly. This module is the real, authoritative
enforcement, called from `POST /users/me/onboarding/complete`
(`app/api/v1/endpoints/user_onboarding.py`), the one place onboarding
data crosses the trust boundary. Deliberately NOT scattered into
unrelated services -- any other place that ever needs this exact rule
should import from here, not re-derive it.
"""

from __future__ import annotations

import datetime as dt

# The product's supported minimum signup age. A single named constant
# (not a magic `13` inline) so the one place this product decision is
# recorded is also the one place it would ever be changed.
MIN_SIGNUP_AGE = 13


def compute_age(date_of_birth: dt.date, as_of: dt.date | None = None) -> int:
    """Deterministic age in whole years as of `as_of` (defaults to
    today, UTC date). Pure function of two dates -- no timezone-naive
    `datetime.now()` call buried inside it, so a caller that already
    has a specific "as of" date (e.g. a test, or a future eligibility
    check against an opportunity's own deadline) gets a reproducible
    answer rather than one that silently depends on wall-clock time.

    Leap-year safe: a person born on Feb 29 is not considered to have
    had their birthday yet in a non-leap year until Mar 1 (comparing
    the `(month, day)` tuple directly handles this correctly without
    any special-casing, since `date(year, 2, 29)` cannot be constructed
    for a non-leap `year` in the first place -- the comparison only ever
    runs against a real calendar date on both sides).
    """
    if as_of is None:
        as_of = dt.datetime.now(dt.timezone.utc).date()

    age = as_of.year - date_of_birth.year
    if (as_of.month, as_of.day) < (date_of_birth.month, date_of_birth.day):
        age -= 1
    return age


class UnderMinimumAgeError(ValueError):
    """Raised by `validate_minimum_age` when the computed age is below
    `MIN_SIGNUP_AGE`. A `ValueError` subclass (not a bare `ValueError`)
    so a caller can catch this specific condition -- e.g. to return a
    distinct, honest 422 response -- without also swallowing an
    unrelated `ValueError` from somewhere else in the same `try` block.
    """

    def __init__(self, age: int) -> None:
        self.age = age
        super().__init__(
            f"Users must be at least {MIN_SIGNUP_AGE} years old "
            f"(computed age: {age})."
        )


def validate_minimum_age(date_of_birth: dt.date, as_of: dt.date | None = None) -> int:
    """Raises `UnderMinimumAgeError` if `date_of_birth` computes to an
    age below `MIN_SIGNUP_AGE` as of `as_of` (defaults to today).
    Returns the computed age on success, so a caller doesn't need to
    call `compute_age` a second time.

    Deliberately takes a `date`, not a `datetime` -- this rule is a
    calendar-date comparison, not a timestamp comparison, so there is
    no timezone-of-day ambiguity to get wrong (a user born at 11pm in
    one timezone is not a different age than one born at 1am the next
    day in another, under this rule -- both are evaluated purely by
    calendar date).
    """
    age = compute_age(date_of_birth, as_of=as_of)
    if age < MIN_SIGNUP_AGE:
        raise UnderMinimumAgeError(age)
    return age
