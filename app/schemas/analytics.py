"""Video Analytics, Watch Time Tracking & Engagement Engine.

Schemas for `POST /api/v1/analytics/heartbeat` (write) and
`GET /api/v1/analytics/video/{video_type}/{video_id}` (read -- disclosed
necessary addition, see that router's own docstring for why a write-only
endpoint can't satisfy the frontend's own explicit "Resume Playback
Feature" requirement on its own).
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import VideoType

# This milestone's own frontend spec sends a heartbeat "exactly every 10
# seconds of continuous playback, or immediately upon onPause and
# onEnded" -- so a genuine, honest `delta_duration_seconds` should almost
# always land somewhere in (0, 10]. This upper bound is set well above
# that (not exactly 10) to tolerate real-world slack: a slow network can
# delay a heartbeat's arrival well past its own 10-second trigger while
# the video kept playing locally the whole time, and this codebase's own
# established posture (see `app/api/v1/endpoints/analytics.py`'s module
# docstring) is to accept that jitter rather than silently truncate a
# late-but-honest heartbeat's watch time. What this bound DOES reject is
# an implausible or adversarial value (e.g. a buggy/malicious client
# claiming an hour of watch time in one pulse) that would both corrupt
# `cumulative_watch_time_seconds` and -- for a COURSE video -- could be
# used to fraudulently race past the 95% completion threshold and farm
# `LESSON_COMPLETION_TOKENS`. Enforced via Pydantic's own `le` validator
# below, which means an out-of-range value is rejected with a clean `422`
# before it ever reaches the database, not silently clamped.
MAX_HEARTBEAT_DELTA_SECONDS = 30.0


class HeartbeatRequest(BaseModel):
    """`POST /api/v1/analytics/heartbeat` request body -- exactly this
    milestone's own literal payload shape: `video_id`, `video_type`,
    `current_time_seconds`, `delta_duration_seconds`, `is_ended`.

    Deliberately does NOT include the video's own duration. See the
    router's own docstring for why: the duration needed to evaluate the
    95%-watched completion threshold is resolved SERVER-SIDE, from the
    video's own `StudentVideo.duration_seconds`/`Lesson.duration_seconds`
    row (looked up via `video_id`+`video_type`), rather than trusted from
    an additional client-supplied field. This keeps the payload exactly
    as small as the ask specifies AND avoids trusting a client-reported
    duration for a completion decision that can trigger a real token
    reward.
    """

    video_id: UUID
    video_type: VideoType
    current_time_seconds: float = Field(
        ge=0, description="The exact second of playback this heartbeat was sent at/near."
    )
    delta_duration_seconds: float = Field(
        ge=0,
        le=MAX_HEARTBEAT_DELTA_SECONDS,
        description="Seconds of ACTUAL playback time elapsed since the previous heartbeat "
        "for this same video (not wall-clock time since the last pulse -- a paused/"
        "buffering video contributes 0 to this value).",
    )
    is_ended: bool = Field(
        default=False, description="True if this heartbeat was sent from the native `ended` event."
    )


class HeartbeatResponse(BaseModel):
    """`POST /api/v1/analytics/heartbeat` response -- "a clean JSON
    validation response" per this milestone's own literal wording. Echoes
    back the row's full post-write state (so the frontend never has to
    make a second round-trip just to confirm what was actually persisted,
    e.g. after `cumulative_watch_time_seconds` was clamped/accumulated
    server-side) plus `tokens_earned`, a disclosed necessary addition:
    without it, a COURSE video crossing the completion threshold and
    silently awarding `LESSON_COMPLETION_TOKENS` in the background would
    give the frontend no way to show the student they just earned tokens,
    the same "encouraging feedback" UX this codebase already treats as a
    first-class concern for `POST /courses/lessons/{id}/complete`.
    """

    video_id: UUID
    video_type: VideoType
    last_position_seconds: float
    cumulative_watch_time_seconds: float
    is_completed: bool
    tokens_earned: int = Field(
        default=0,
        ge=0,
        description="LESSON_COMPLETION_TOKENS if this heartbeat is what newly completed a "
        "COURSE video for the first time; 0 for every other case (UGC videos, an "
        "already-completed video, or a heartbeat that didn't cross the threshold).",
    )


class VideoPlaybackProgressOut(BaseModel):
    """`GET /api/v1/analytics/video/{video_type}/{video_id}` response --
    disclosed necessary read-side endpoint (see the router's own
    docstring). Same shape as the persisted row itself, minus `user_id`
    (implicit -- always the authenticated caller) and `id`/timestamps
    (irrelevant to a player component that only needs "where do I resume
    from").
    """

    model_config = ConfigDict(from_attributes=True)

    video_id: UUID
    video_type: VideoType
    last_position_seconds: float
    cumulative_watch_time_seconds: float
    is_completed: bool
