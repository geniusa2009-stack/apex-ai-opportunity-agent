"""Application Intelligence step: response shapes for
`GET /tracker/{id}/requirements` and `GET /tracker/{id}/intelligence`.

Built directly from the real dataclasses in
`app/services/application_requirements.py` / `application_intelligence.py`
-- a public API shape, not a second copy of that logic (same posture as
`app/schemas/user_intelligence.py` from the immediately preceding phase).
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel


class RequirementOut(BaseModel):
    key: str
    label: str
    status: str  # required | conditional | optional
    source_field: str
    evidence_snippet: str
    completion: str  # complete | missing
    completed_at: dt.datetime | None


class RequirementsOut(BaseModel):
    tracker_id: uuid.UUID
    opportunity_id: uuid.UUID | None
    requirements: list[RequirementOut]


class ChecklistTaskOut(BaseModel):
    task_id: int
    task_name: str
    is_completed: bool


class OpportunityChangeOut(BaseModel):
    field_name: str
    old_value: str | None
    new_value: str | None
    changed_at: dt.datetime


class ApplicationIntelligenceSnapshotOut(BaseModel):
    tracker_id: uuid.UUID
    status: str
    scholarship_name: str
    institution: str | None
    opportunity_id: uuid.UUID | None
    opportunity_title: str | None
    deadline: dt.datetime | None
    deadline_state: str
    requirements: list[RequirementOut]
    ai_checklist: list[ChecklistTaskOut]
    recent_opportunity_changes: list[OpportunityChangeOut]
    is_submitted: bool
    submitted_at: dt.datetime | None
    generated_at: dt.datetime
