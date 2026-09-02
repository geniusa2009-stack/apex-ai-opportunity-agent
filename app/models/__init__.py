"""Import every model module here so that:

1. `Base.metadata` knows about all tables (required for Alembic autogenerate).
2. String-based relationship() forward references (e.g. "UserBookmark")
   resolve correctly when SQLAlchemy configures mappers.

Anything importing `app.models` (or `app.db.base.Base` after this package
has been imported once) sees the full picture.
"""

from app.models.agent_alert import AgentAlert
from app.models.alumni import Alumnus
from app.models.analytics import VideoPlaybackLog
from app.models.application_tracker import ApplicationTracker
from app.models.badges import Badge, GamificationProfile, UserBadge
from app.models.bookmark import UserBookmark
from app.models.budget_planner import BudgetPlanner
from app.models.career_compass import CareerCompassResult
from app.models.chat import ChatMessage, ChatParticipant, ChatRoom
from app.models.chat_moderation import ChatMessageReport, ChatMessageReportEntry
from app.models.deadline_alert import DeadlineAlert
from app.models.decision_matrix import DecisionMatrix
from app.models.expat_survival import ExpatSurvival
from app.models.features import Certificate, LessonStudyAid, MentorshipRelation
from app.models.focus_room import FocusSession
from app.models.gamification import StudentAccountability
from app.models.guild_moderation import GuildContentReport, GuildContentReportEntry
from app.models.guilds import Guild, GuildComment, GuildPost, GuildQuest
from app.models.live_session import LiveSession
from app.models.moderation_appeal import ModerationAppeal
from app.models.notification import Notification
from app.models.onboarding import OnboardingChecklistProgress
from app.models.opportunity import Opportunity
from app.models.parent_pitch import ParentPitch
from app.models.courses import Course, CourseModule, Lesson, LessonProgress, UserEnrollment
from app.models.social import AchievementCongrats, AchievementPost, StudyDuel, UserConnection
from app.models.source import Source
from app.models.student_memory import StudentMemory
from app.models.student_videos import StudentVideo
from app.models.user import User
from app.models.user_history import UserHistory

__all__ = [
    "User",
    "Source",
    "Opportunity",
    "UserBookmark",
    "Alumnus",
    "AgentAlert",
    "UserHistory",
    "StudentMemory",
    "ApplicationTracker",
    "DeadlineAlert",
    "StudentAccountability",
    "GamificationProfile",
    "Badge",
    "UserBadge",
    "FocusSession",
    "Guild",
    "GuildPost",
    "GuildComment",
    "GuildContentReport",
    "GuildContentReportEntry",
    "GuildQuest",
    "ParentPitch",
    "DecisionMatrix",
    "BudgetPlanner",
    "ExpatSurvival",
    "UserConnection",
    "AchievementPost",
    "AchievementCongrats",
    "StudyDuel",
    "Course",
    "CourseModule",
    "Lesson",
    "UserEnrollment",
    "LessonProgress",
    "StudentVideo",
    "VideoPlaybackLog",
    "ChatRoom",
    "ChatParticipant",
    "ChatMessage",
    "ChatMessageReport",
    "ChatMessageReportEntry",
    "Notification",
    "Certificate",
    "LessonStudyAid",
    "MentorshipRelation",
    "LiveSession",
    "ModerationAppeal",
    "OnboardingChecklistProgress",
]
