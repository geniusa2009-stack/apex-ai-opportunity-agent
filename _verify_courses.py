"""Throwaway verification script for Ecosystem Features 13 & 14 (AI-Powered
LMS & Interactive Academy) -- not part of the application, and not
imported by anything in `app/`.

It ran an in-memory SQLite check of `app/services/course_service.py`
covering course/module/lesson authoring, catalog listing, premium-content
masking pre-enrollment (title visible, body hidden) vs. free-course full
visibility, token-gated enrollment (success, idempotent re-enroll,
insufficient-token failure with no partial charge), the enrollment-required
guard on `complete_lesson`/`ask_course_copilot`/`generate_lesson_quiz`,
idempotent lesson completion (no double token award on a repeat call), the
detail view reflecting completed progress, a mocked copilot answer and a
mocked 3-question/4-option quiz for an enrolled student, and 404s for
nonexistent courses/lessons -- 19 assertions, all passed. A separate
follow-up check confirmed the router's `_require_admin` gate correctly
blocks a non-superuser with 403 and passes a superuser through.

Left in this reduced, inert form rather than actually deleted: this
sandbox's `rm`/`os.remove`/`mv` all return `PermissionError: Operation not
permitted` for this file specifically (same restriction already hit and
disclosed for `app/api/v1/endpoints/_removed_auth.py.bak` during the
Supabase integration step, and for `_verify_social.py` in the immediately
preceding step -- see the README's notes on both). Safe to delete by hand
outside this sandbox; it has no other effect on the app.
"""
