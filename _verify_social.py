"""Throwaway verification script for Ecosystem Features 11 & 12 (Social
Hub, Share Cards & Study Duels) -- not part of the application, and not
imported by anything in `app/`.

It ran an in-memory SQLite check of `app/services/social_service.py`
covering feed-privacy visibility rules, the friend-request lifecycle
(mutual auto-accept, duplicate/self-request guards), the daily
congrats-token cap, and the full study-duel wager lifecycle (challenge,
accept, decline-refund, complete-with-winner, tie-split, and the various
authorization/insufficient-token guards) -- all 18 assertions passed.

Left in this reduced, inert form rather than actually deleted: this
sandbox's `rm`/`os.remove`/`mv` all return `PermissionError: Operation
not permitted` for this file specifically (same restriction already hit
and disclosed for `app/api/v1/endpoints/_removed_auth.py.bak` during the
Supabase integration step -- see the README's note on that). Safe to
delete by hand outside this sandbox; it has no other effect on the app.
"""
