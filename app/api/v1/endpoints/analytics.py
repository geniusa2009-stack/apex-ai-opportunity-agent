"""Router layer for the Video Analytics, Watch Time Tracking &
Engagement Engine.

No shared `prefix` -- same posture as `student_videos.py`/`media_router.py`
-- each route's full literal path is written directly in its own
decorator.

Routing-precedence check performed (same discipline as every other
router in this codebase): `POST /analytics/heartbeat` and
`GET /analytics/video/{video_type}/{video_id}` occupy disjoint literal
second-segments ("heartbeat" vs. "video") and different HTTP methods
besides -- there is no path-template ambiguity between them, or with any
other router in this codebase (no other router owns anything under
`/analytics`). Confirmed empirically anyway (see the README's
verification section), not just reasoned about.

Disclosed necessary addition beyond this milestone's own literal
endpoint list: `GET /analytics/video/{video_type}/{video_id}`. The ask's
own component 2 (frontend) explicitly requires a "Resume Playback
Feature" that "checks if the backend has a historical last_position_seconds
for this user + video" the moment the player mounts -- but the ask's own
component 1 (backend) only ever specifies the WRITE side of this system
(`POST /analytics/heartbeat`). A write-only endpoint cannot, on its own,
answer "what was this user's last position for this video" -- there is
no other existing endpoint in this codebase that exposes per-user,
per-video playback state either (`StudentVideoPrivateFeedbackOut`/
`LessonProgressOut` expose completion/feedback, never a resume position).
This GET route is the minimum necessary read path to make the explicitly
requested frontend feature buildable at all, added in the same spirit as
every other "this literal ask can't fully work without one small,
disclosed backend addition" gap already filled elsewhere in this
codebase (e.g. `StudentVideoMyUploadOut.hls_master_url`, disclosed there
for an analogous reason).
"""

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.models.enums import VideoType
from app.schemas.analytics import HeartbeatRequest, HeartbeatResponse, VideoPlaybackProgressOut
from app.services.analytics_service import VideoNotFoundError, get_playback_progress, record_heartbeat

logger = logging.getLogger(__name__)

router = APIRouter(tags=["analytics"])


@router.post("/analytics/heartbeat", response_model=HeartbeatResponse)
async def post_heartbeat(
    payload: HeartbeatRequest, current_user: CurrentUser, db: DbSession
) -> HeartbeatResponse:
    """Authenticated (`current_user` per this milestone's own literal
    "Protected by authentication" requirement). Delegates the entire
    atomic upsert + completion-threshold business logic to
    `analytics_service.record_heartbeat` -- see that function's own
    docstring for the full disclosed reasoning behind each step.

    `404` if `payload.video_id` doesn't resolve to a real row in the
    table `payload.video_type` implies -- same "can't reference a real
    thing" posture as every other `{video_id}`-parameterized route in
    this codebase (e.g. `student_videos.py`'s `DELETE /student/videos/
    {video_id}`), applied here to a body-carried id instead of a
    path-carried one since this milestone's own literal payload shape
    puts `video_id` in the request body, not the URL.
    """
    try:
        return await record_heartbeat(db, current_user.id, payload)
    except VideoNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No {payload.video_type.value} video exists with id {payload.video_id}.",
        )


@router.get("/analytics/video/{video_type}/{video_id}", response_model=VideoPlaybackProgressOut)
async def get_video_playback_progress(
    video_type: VideoType, video_id: UUID, current_user: CurrentUser, db: DbSession
) -> VideoPlaybackProgressOut:
    """Disclosed necessary read-side addition -- see this module's own
    docstring. Always `200`, never `404` -- a viewer with no playback
    history for this video yet is a normal, expected state (see
    `analytics_service.get_playback_progress`'s own docstring), not an
    error. Deliberately does NOT 404 on an unknown/nonexistent `video_id`
    either (unlike the heartbeat endpoint) -- this is a read-only,
    best-effort "where should the player resume from" lookup, and a
    nonexistent video simply has no playback history to report, which is
    indistinguishable (and equally harmless) from "this video exists but
    has never been watched" from this endpoint's own narrow point of
    view. The player component calling this only ever does so for a
    `video_id` it already knows is real (it's the very video it's about
    to play), so this isn't a meaningful validation gap in practice.
    """
    return await get_playback_progress(db, current_user.id, video_id, video_type)
