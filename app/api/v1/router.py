from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.endpoints.admin_activity_log import router as admin_activity_log_router
from app.api.v1.endpoints.admin_blacklist import router as admin_blacklist_router
from app.api.v1.endpoints.admin_guild_reports import router as admin_guild_reports_router
from app.api.v1.endpoints.admin_signups import router as admin_signups_router
from app.api.v1.endpoints.admin_chat_reports import router as admin_chat_reports_router
from app.api.v1.endpoints.admin_high_risk import router as admin_high_risk_router
from app.api.v1.endpoints.admin_canned_responses import router as admin_canned_responses_router
from app.api.v1.endpoints.admin_case_notes import router as admin_case_notes_router
from app.api.v1.endpoints.admin_dangerous_actions import router as admin_dangerous_actions_router
from app.api.v1.endpoints.admin_escalations import router as admin_escalations_router
from app.api.v1.endpoints.admin_opportunities import router as admin_opportunities_router
from app.api.v1.endpoints.admin_repeat_offenders import router as admin_repeat_offenders_router
from app.api.v1.endpoints.moderation_appeals import router as moderation_appeals_router
from app.api.v1.endpoints.admin_claims import router as admin_claims_router
from app.api.v1.endpoints.admin_moderation import router as admin_moderation_router
from app.api.v1.endpoints.admin_roles import router as admin_roles_router
from app.api.v1.endpoints.admin_stats import router as admin_stats_router
from app.api.v1.endpoints.advanced import router as advanced_router
from app.api.v1.endpoints.ai_cv_builder import router as ai_cv_builder_router
from app.api.v1.endpoints.ai_tools import router as ai_tools_router
from app.api.v1.endpoints.alumni import router as alumni_router
from app.api.v1.endpoints.alumni_connector import router as alumni_connector_router
from app.api.v1.endpoints.analytics import router as analytics_router
from app.api.v1.endpoints.appeal_drafter import router as appeal_drafter_router
from app.api.v1.endpoints.application_tracker import router as application_tracker_router
from app.api.v1.endpoints.autofill import router as autofill_router
# `app.api.v1.endpoints.auth` (local POST /auth/register + POST
# /auth/login) was removed as part of the Supabase integration step --
# see the README section on that step for why, and for a note on why
# the file itself was renamed to `_removed_auth.py.bak` rather than
# truly deleted (a sandbox restriction on `rm`, disclosed there).
from app.api.v1.endpoints.bookmarks import router as bookmarks_router
from app.api.v1.endpoints.budget_planner import router as budget_planner_router
from app.api.v1.endpoints.career_compass import router as career_compass_router
from app.api.v1.endpoints.chat import router as chat_router
from app.api.v1.endpoints.coach import router as coach_router
from app.api.v1.endpoints.competition_scout import router as competition_scout_router
from app.api.v1.endpoints.courses import router as courses_router
from app.api.v1.endpoints.cultural_localizer import router as cultural_localizer_router
from app.api.v1.endpoints.culture_shock import router as culture_shock_router
from app.api.v1.endpoints.dashboards import router as dashboards_router
from app.api.v1.endpoints.deadline_alerts import router as deadline_alerts_router
from app.api.v1.endpoints.decision_matrix import router as decision_matrix_router
from app.api.v1.endpoints.deduplicator import router as deduplicator_router
from app.api.v1.endpoints.devils_advocate import router as devils_advocate_router
from app.api.v1.endpoints.document_checker import router as document_checker_router
from app.api.v1.endpoints.essay_humanizer import router as essay_humanizer_router
from app.api.v1.endpoints.essay_sparker import router as essay_sparker_router
from app.api.v1.endpoints.exchange_advisor import router as exchange_advisor_router
from app.api.v1.endpoints.expat_survival import router as expat_survival_router
from app.api.v1.endpoints.extracurriculars import router as extracurriculars_router
from app.api.v1.endpoints.extractor import router as extractor_router
from app.api.v1.endpoints.focus import router as focus_router
from app.api.v1.endpoints.gamification import router as gamification_router
from app.api.v1.endpoints.grade_converter import router as grade_converter_router
from app.api.v1.endpoints.grader import router as grader_router
from app.api.v1.endpoints.guilds import router as guilds_router
from app.api.v1.endpoints.high_school_resume import router as high_school_resume_router
from app.api.v1.endpoints.history import router as history_router
from app.api.v1.endpoints.interview import router as interview_router
from app.api.v1.endpoints.live_sessions import router as live_sessions_router
from app.api.v1.endpoints.multi_agent_interview import router as multi_agent_interview_router
from app.api.v1.endpoints.media_router import router as media_router
from app.api.v1.endpoints.mentorship import router as mentorship_router
from app.api.v1.endpoints.notifications import router as notifications_router
from app.api.v1.endpoints.onboarding import router as onboarding_router
from app.api.v1.endpoints.practice_cases import router as practice_cases_router
from app.api.v1.endpoints.admin_shifts import router as admin_shifts_router
from app.api.v1.endpoints.parent_guide import router as parent_guide_router
from app.api.v1.endpoints.parent_pitch import router as parent_pitch_router
from app.api.v1.endpoints.planner import router as planner_router
from app.api.v1.endpoints.polisher import router as polisher_router
from app.api.v1.endpoints.portfolio import router as portfolio_router
from app.api.v1.endpoints.portfolio_curator import router as portfolio_curator_router
from app.api.v1.endpoints.probability import router as probability_router
from app.api.v1.endpoints.professor import router as professor_router
from app.api.v1.endpoints.quest_planner import router as quest_planner_router
from app.api.v1.endpoints.recommendation_writer import router as recommendation_writer_router
from app.api.v1.endpoints.recommendations import router as recommendations_router
from app.api.v1.endpoints.research_mentor import router as research_mentor_router
from app.api.v1.endpoints.safety_filter import router as safety_filter_router
from app.api.v1.endpoints.scholar_agent import router as scholar_agent_router
from app.api.v1.endpoints.scholarship_recovery import router as scholarship_recovery_router
from app.api.v1.endpoints.skill_gap import router as skill_gap_router
from app.api.v1.endpoints.social import router as social_router
from app.api.v1.endpoints.stem_coach import router as stem_coach_router
from app.api.v1.endpoints.student_videos import router as student_videos_router
from app.api.v1.endpoints.summer_programs import router as summer_programs_router
from app.api.v1.endpoints.tagger import router as tagger_router
from app.api.v1.endpoints.teacher_guide import router as teacher_guide_router
from app.api.v1.endpoints.teen_interview import router as teen_interview_router
from app.api.v1.endpoints.test_pathway import router as test_pathway_router
from app.api.v1.endpoints.trend_predictor import router as trend_predictor_router
from app.api.v1.endpoints.undergrad_targeter import router as undergrad_targeter_router
from app.api.v1.endpoints.users import router as users_router
from app.api.v1.endpoints.uwc_strategist import router as uwc_strategist_router
from app.api.v1.endpoints.voice_coach import router as voice_coach_router
from app.api.v1.endpoints.voice_scholar import router as voice_scholar_router
from app.api.v1.endpoints.wallet import router as wallet_router
from app.api.v1.endpoints.web_explorer import router as web_explorer_router
from app.api.v1.endpoints.youtube import router as youtube_router
from app.api.v1.endpoints.opportunities import router as opportunities_router
from app.api.v1.endpoints.pdf_filler import router as pdf_filler_router
from app.api.v1.endpoints.peer_review import router as peer_review_router
from app.db.session import get_db

api_router = APIRouter()


@api_router.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)) -> dict:
    """Basic liveness + DB connectivity check."""
    result = await db.execute(text("SELECT 1"))
    db_ok = result.scalar_one() == 1
    return {"status": "ok", "database": "connected" if db_ok else "unreachable"}


api_router.include_router(opportunities_router)
api_router.include_router(bookmarks_router)
api_router.include_router(ai_tools_router)
api_router.include_router(advanced_router)
api_router.include_router(chat_router)
api_router.include_router(skill_gap_router)
api_router.include_router(interview_router)
api_router.include_router(grader_router)
api_router.include_router(planner_router)
api_router.include_router(tagger_router)
api_router.include_router(polisher_router)
api_router.include_router(recommendations_router)
api_router.include_router(deduplicator_router)
api_router.include_router(extractor_router)
api_router.include_router(professor_router)
api_router.include_router(voice_coach_router)
api_router.include_router(portfolio_router)
api_router.include_router(document_checker_router)
api_router.include_router(youtube_router)
api_router.include_router(users_router)
api_router.include_router(autofill_router)
api_router.include_router(devils_advocate_router)
api_router.include_router(alumni_router)
api_router.include_router(probability_router)
api_router.include_router(web_explorer_router)
api_router.include_router(multi_agent_interview_router)
api_router.include_router(cultural_localizer_router)
api_router.include_router(scholar_agent_router)
api_router.include_router(peer_review_router)
api_router.include_router(pdf_filler_router)
api_router.include_router(trend_predictor_router)
api_router.include_router(recommendation_writer_router)
api_router.include_router(quest_planner_router)
api_router.include_router(appeal_drafter_router)
api_router.include_router(voice_scholar_router)
api_router.include_router(extracurriculars_router)
api_router.include_router(competition_scout_router)
api_router.include_router(summer_programs_router)
api_router.include_router(test_pathway_router)
api_router.include_router(stem_coach_router)
api_router.include_router(exchange_advisor_router)
api_router.include_router(uwc_strategist_router)
api_router.include_router(high_school_resume_router)
api_router.include_router(essay_sparker_router)
api_router.include_router(teacher_guide_router)
api_router.include_router(grade_converter_router)
api_router.include_router(parent_guide_router)
api_router.include_router(safety_filter_router)
api_router.include_router(research_mentor_router)
api_router.include_router(portfolio_curator_router)
api_router.include_router(teen_interview_router)
api_router.include_router(culture_shock_router)
api_router.include_router(undergrad_targeter_router)
api_router.include_router(alumni_connector_router)
api_router.include_router(scholarship_recovery_router)
api_router.include_router(history_router)
api_router.include_router(application_tracker_router)
api_router.include_router(deadline_alerts_router)
api_router.include_router(essay_humanizer_router)
api_router.include_router(coach_router)
api_router.include_router(wallet_router)
api_router.include_router(focus_router)
api_router.include_router(guilds_router)
api_router.include_router(parent_pitch_router)
api_router.include_router(decision_matrix_router)
api_router.include_router(budget_planner_router)
api_router.include_router(expat_survival_router)
api_router.include_router(social_router)
# `student_videos_router` (Student Video Profiles & AI-Moderated Upload
# System) has no path-template collision with any other router -- its
# `/student/{student_id}/videos` and `/student/videos/*` paths put the
# literal "videos" segment at DIFFERENT positions, unlike `/courses/*`
# below -- so, unlike `media_router`, its registration order here doesn't
# matter. Placed here anyway, out of the router-file's usual strict
# alphabetical import order, simply to keep it near the other Ecosystem/
# infrastructure-tier routers rather than requiring a second pass through
# this whole file -- confirmed empirically (see the README's verification
# section), not just reasoned about.
api_router.include_router(student_videos_router)
# `admin_moderation_router` (Admin Override Dashboard & AI Decision
# Correction System) -- its two paths, `/admin/videos/review-queue` and
# `/admin/videos/{video_id}/override`, share no literal path segment
# with any other router's routes at the same position (`media_router`'s
# only admin route is under `/admin/courses/...`, a different second
# segment) -- registration order relative to every other router here is
# provably irrelevant. Placed immediately after `student_videos_router`
# simply because it operates on the same `StudentVideo` model, same
# rationale as that router's own placement note above. Confirmed
# empirically (see the README's verification section), not just
# reasoned about.
api_router.include_router(admin_moderation_router)
# `admin_roles_router` (Real Tiered Role System step) -- its two paths,
# `/admin/users/{user_id}/role` and `/admin/team`, diverge at the SECOND
# path segment (`users`/`team`) from every other `admin/*` route already
# registered above/below (`admin/videos/*`, `admin/courses/*`) -- no
# path-template ambiguity, registration order here is provably
# irrelevant. Placed immediately after `admin_moderation_router` simply
# because both gate access to the same moderation surface area.
api_router.include_router(admin_roles_router)
# `admin_activity_log_router` (Automatic Moderation Activity Log step)
# -- its two paths, `/admin/activity-log/{user_id}` and
# `/admin/activity-log`, diverge at the SECOND path segment
# (`activity-log`) from every other `admin/*` route already registered
# above -- no path-template ambiguity, registration order here is
# provably irrelevant. Placed immediately after `admin_roles_router`
# simply because both are Owner-only surfaces over the same moderation
# subsystem.
api_router.include_router(admin_activity_log_router)
# `admin_claims_router` (Claim System step) -- its two paths (`POST`/
# `DELETE /admin/moderation/{item_type}/{item_id}/claim`) live under a
# literal `admin/moderation/...` prefix that diverges at the SECOND path
# segment (`moderation`) from every other `admin/*` route already
# registered above (`admin/videos/*`, `admin/users/*`, `admin/team`,
# `admin/activity-log*`) -- no path-template ambiguity, registration
# order here is provably irrelevant. Placed immediately after
# `admin_activity_log_router` simply because both are recent additions
# to the same moderation subsystem.
api_router.include_router(admin_claims_router)
# `admin_escalations_router` (Escalation Path Between Volunteers) --
# `POST`/`DELETE /admin/moderation/{item_type}/{item_id}/escalate`
# shares the SAME `admin/moderation/{item_type}/{item_id}/...` prefix
# as `admin_claims_router` immediately above, diverging only at the
# final literal segment (`escalate` vs. `claim`) -- FastAPI matches on
# the full literal path, so this is no more ambiguous than `claim`/
# `release` already being two different HTTP methods on the identical
# path. `GET /admin/escalations` is a brand-new top-level literal
# sharing no segment position with any other route already registered.
# Registration order is provably irrelevant either way; placed
# immediately after `admin_claims_router` since both are the same
# `{item_type}/{item_id}` dispatch pattern.
api_router.include_router(admin_escalations_router)
# `admin_canned_responses_router` (Canned Responses / Templates) -- its
# real paths, `/admin/canned-responses*`, are brand-new top-level
# literals sharing no segment position with any other router already
# registered -- no path-template ambiguity, registration order is
# provably irrelevant.
api_router.include_router(admin_canned_responses_router)
# `admin_case_notes_router` (Internal Notes on Cases) -- shares the
# SAME `admin/moderation/{item_type}/{item_id}/...` prefix as
# `admin_claims_router`/`admin_escalations_router`, diverging at the
# final literal segment (`notes` vs. `claim`/`escalate`) -- same "no
# ambiguity, FastAPI matches the full literal path" reasoning as
# `admin_escalations_router`'s own registration comment above.
api_router.include_router(admin_case_notes_router)
# `admin_dangerous_actions_router` (Two-Person Confirmation for
# Dangerous Actions) -- its `approve` route is resource-id-based
# (`/admin/moderation/dangerous-actions/{confirmation_id}/approve`),
# and its `override` route shares the `{item_type}/{item_id}/...`
# prefix but diverges at BOTH the fourth segment (`dangerous-actions`,
# a literal, vs. `escalate`/`notes`/`claim`'s own final-segment verbs)
# AND has more total segments (`.../dangerous-actions/{action}/
# override`) -- no route already registered shares this shape, so
# there is no path-template ambiguity regardless of registration
# order. See `admin_dangerous_actions.py`'s own module docstring for
# the full reasoning.
api_router.include_router(admin_dangerous_actions_router)
# `admin_stats_router` (Phase 5 -- Contribution Stats + The 3 Views) --
# `/admin/team/summary` diverges from the EXISTING `/admin/team`
# (`admin_roles_router`, above) at the THIRD path segment (`/admin/team`
# has no further segment to collide with); `/admin/today` is a brand-new
# top-level literal under `admin/` sharing no segment position with any
# other `admin/*` route registered above -- no path-template ambiguity
# either way, registration order here is provably irrelevant. Placed
# immediately after `admin_claims_router` simply because it's the most
# recent addition to the same moderation subsystem.
api_router.include_router(admin_stats_router)
# `admin_chat_reports_router` (Extend Moderation to Chat Messages -- the
# "second real queue" `admin_claims_router`'s own module docstring left
# open) -- its paths, `/admin/chat/reports*`, diverge at the SECOND path
# segment (`chat`) from every other `admin/*` route already registered
# above (`admin/videos/*`, `admin/users/*`, `admin/team*`,
# `admin/activity-log*`, `admin/moderation/*`) -- no path-template
# ambiguity, registration order here is provably irrelevant. Placed
# immediately after `admin_stats_router` simply because it's the most
# recent addition to the same moderation subsystem.
api_router.include_router(admin_chat_reports_router)
# `admin_repeat_offenders_router` (Repeat-Offender Detection) -- its one
# real path, `/admin/flagged-users`, is a brand-new top-level literal
# under `admin/` sharing no segment position with any other `admin/*`
# route registered above -- no path-template ambiguity, registration
# order here is provably irrelevant. Placed immediately after
# `admin_chat_reports_router` simply because it's the most recent
# addition to the same moderation subsystem and reads its real,
# durable audit-log data.
api_router.include_router(admin_repeat_offenders_router)
# `admin_high_risk_router` (High-Risk-Content View) -- its one real
# path, `/admin/high-risk`, is a brand-new top-level literal under
# `admin/` sharing no segment position with any other `admin/*` route
# registered above -- no path-template ambiguity, registration order
# here is provably irrelevant. Placed immediately after
# `admin_repeat_offenders_router` simply because it's the most recent
# addition to the same moderation subsystem and reuses its real,
# already-tested flagged-users data directly.
api_router.include_router(admin_high_risk_router)
# `admin_opportunities_router` (Review of Aggregated Opportunities Before
# Publish) -- included right after `admin_high_risk_router` simply
# because it's the most recent addition, same "no real ordering
# significance" posture every prior addition to this list has already
# disclosed.
api_router.include_router(admin_opportunities_router)
# `moderation_appeals_router` (Transparency: Notify Users + Real Appeal
# Path) -- its real paths, `/moderation/appeals*` and `/admin/appeals*`,
# are both brand-new top-level literals sharing no segment position with
# any other route registered above -- no path-template ambiguity,
# registration order here is provably irrelevant. Placed immediately
# after `admin_repeat_offenders_router` simply because it's the most
# recent addition to the same moderation subsystem.
api_router.include_router(moderation_appeals_router)
# `admin_blacklist_router` (General Content Blacklist / Pattern Auto-
# Trigger) -- its real paths, `/admin/blacklist/patterns*` and
# `/admin/blacklist/triggers`, are brand-new top-level literals sharing
# no segment position with any other route registered above -- no
# path-template ambiguity, registration order here is provably
# irrelevant. Placed immediately after `moderation_appeals_router`
# simply because it's the most recent addition to the same moderation
# subsystem, same "no real ordering significance" posture every prior
# addition to this list has already disclosed.
api_router.include_router(admin_blacklist_router)
# `admin_signups_router` (Review of Suspicious New Signups) -- its one
# real path, `/admin/signups/review`, is a brand-new top-level literal
# sharing no segment position with any other route -- no path-template
# ambiguity, registration order here is provably irrelevant. Placed
# immediately after `admin_blacklist_router` simply because it's the
# most recent addition to the same moderation subsystem, same "no real
# ordering significance" posture every prior addition to this list has
# already disclosed.
api_router.include_router(admin_signups_router)
# `admin_guild_reports_router` (Review of Guild Content) -- its paths
# all start with the literal segment `admin/guild/reports`, distinct
# at the second/third path segment from every other `admin/*` router
# already registered, so there is no path-template ambiguity
# regardless of registration order.
api_router.include_router(admin_guild_reports_router)
# `onboarding_router` (New-Volunteer Onboarding Checklist) -- its real
# paths, `/me/onboarding*` and `/admin/onboarding/team`, are brand-new
# top-level literals sharing no segment position with any other route
# already registered (no other router owns a literal `me/onboarding` or
# `admin/onboarding` prefix) -- no path-template ambiguity, registration
# order here is provably irrelevant. Placed immediately after
# `admin_guild_reports_router` simply because it's the most recent
# addition to the same moderation subsystem.
api_router.include_router(onboarding_router)
# `practice_cases_router` (Training / Practice Mode for New Volunteers)
# -- its real paths, `/me/practice*`, are brand-new top-level literals
# sharing no segment position with any other router already registered
# (no other router owns a literal `me/practice` prefix) -- no path-
# template ambiguity, registration order is provably irrelevant.
# Placed immediately after `onboarding_router` since this feature's own
# sixth onboarding step hooks directly into it (see `app/models/
# onboarding.py`'s own docstring on `complete_practice_cases`).
api_router.include_router(practice_cases_router)
# `admin_shifts_router` (Volunteer Coverage / Shift Schedule) -- its
# real paths, `/admin/shifts*`, are brand-new top-level literals
# sharing no segment position with any other router already
# registered (no other router owns a literal `admin/shifts` prefix)
# -- no path-template ambiguity, registration order is provably
# irrelevant.
api_router.include_router(admin_shifts_router)
# `media_router` MUST be included BEFORE `courses_router` -- disclosed
# routing-precedence requirement, Core Video Media Processing Pipeline
# step. `media_router` owns `GET /courses/recommendations`, which shares
# its `/courses` literal prefix with `courses_router`'s `GET
# /courses/{course_id}`. FastAPI/Starlette matches routes in registration
# order using each route's raw path TEMPLATE, not the target function's
# type hints -- `{course_id}` has no `:uuid` converter in its path
# string, so it matches ANY single segment, including the literal string
# "recommendations". If `courses_router` were registered first,
# `/courses/recommendations` would match `/courses/{course_id}` first and
# fail as a 422 (invalid UUID) instead of ever reaching the intended
# route. See `media_router.py`'s own module docstring for the full
# explanation.
api_router.include_router(media_router)
api_router.include_router(courses_router)
# `analytics_router` (Video Analytics, Watch Time Tracking & Engagement
# Engine) -- both its paths, `/analytics/heartbeat` and `/analytics/
# video/{video_type}/{video_id}`, live under a literal `/analytics`
# prefix no other router in this codebase uses at all, so registration
# order here is provably irrelevant. Placed last simply because it's the
# most recently added router, same low-stakes placement rationale as
# `student_videos_router`/`admin_moderation_router` above.
api_router.include_router(analytics_router)
# `gamification_router` (Gamification: Daily Streaks, Badges, and
# Leaderboard) -- all three of its paths live under a literal
# `/gamification` prefix no other router in this codebase uses at all,
# so registration order here is provably irrelevant, same posture as
# `analytics_router` immediately above. Placed last for the same
# low-stakes reason: most recently added router.
api_router.include_router(gamification_router)
# `notifications_router` (Smart Notifications Engine) -- all four of its
# paths (`GET /notifications`, `PUT /notifications/read-all`, `PUT
# /notifications/{id}/read`, `WS /notifications/ws`) live under a
# literal `/notifications` prefix no other router in this codebase uses
# at all, so registration order here is provably irrelevant, same
# posture as `gamification_router` immediately above. Placed last for
# the same low-stakes reason: most recently added router. (Note: the
# `read-all` vs. `{notification_id}/read` ordering that DOES matter is
# handled entirely WITHIN `notifications.py`'s own route declaration
# order -- see that file's own comment on it -- not here.)
api_router.include_router(notifications_router)
# `dashboards_router` (Student Dashboard & Admin Control Panel) -- both
# its paths (`GET /dashboards/student`, `GET /dashboards/admin`) live
# under a literal `/dashboards` prefix no other router in this codebase
# uses at all, so registration order here is provably irrelevant, same
# posture as `notifications_router` immediately above. Placed last for
# the same low-stakes reason: most recently added router. This router is
# a pure READ aggregation layer over five already-registered feature
# routers above (gamification, notifications, student_videos,
# admin_moderation, chat via chat_security) -- it owns no models or
# mutations of its own; the two admin ACTIONS this same step adds
# ("Reject & Delete" media purge, "Unban Early") are registered under
# `admin_moderation_router`/`chat_router` themselves instead, since they
# mutate resources those routers already own (see each endpoint's own
# docstring).
api_router.include_router(dashboards_router)
# `mentorship_router` (Hyper-Personalized Platform step) -- all four of
# its paths (`POST /mentorship/request`, `POST /mentorship/{id}/respond`,
# `POST /mentorship/{id}/complete`, `GET /mentorship/mine`) live under a
# literal `/mentorship` prefix no other router in this codebase uses at
# all, so registration order here is provably irrelevant, same posture as
# `dashboards_router` immediately above. Placed last for the same
# low-stakes reason: most recently added router.
api_router.include_router(mentorship_router)
api_router.include_router(live_sessions_router)
# `ai_cv_builder_router` (Hyper-Personalized Platform step) -- its single
# path, `GET /ai/cv-builder`, lives under a literal `/ai` prefix no other
# router in this codebase uses (`ai_tools_router` uses `/ai-tools`, a
# different literal string -- FastAPI/Starlette path matching is exact
# per segment, so `/ai` and `/ai-tools` never collide regardless of
# registration order). Placed last for the same low-stakes reason as
# `mentorship_router` immediately above: most recently added router.
api_router.include_router(ai_cv_builder_router)
# `career_compass_router` (Career Compass -- real deterministic RIASEC
# interest inventory) -- both its paths (`GET /career-compass/questions`,
# `POST /career-compass/submit`, `GET /career-compass/me`) live under a
# literal `/career-compass` prefix no other router in this codebase uses
# at all, so registration order here is provably irrelevant, same posture
# as `mentorship_router`/`ai_cv_builder_router` above. Placed last for the
# same low-stakes reason: most recently added router.
api_router.include_router(career_compass_router)
