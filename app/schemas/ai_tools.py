from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class DocumentGenerationRequest(BaseModel):
    """Shared request shape for CV and motivation letter generation."""

    opportunity_id: UUID
    user_background: str = Field(
        min_length=20,
        max_length=5000,
        description="Free-text background: education, experience, relevant history.",
    )
    key_achievements: str | None = Field(
        default=None,
        max_length=3000,
        description="Notable achievements, skills, or qualifications to emphasize.",
    )
    additional_context: str | None = Field(
        default=None,
        max_length=2000,
        description="Anything else relevant -- tone preferences, specific points to include, etc.",
    )


class RecommendationLetterRequest(DocumentGenerationRequest):
    """Recommendation letters additionally need to know who's ostensibly
    writing it -- required here (not optional), since a recommendation
    letter without a stated recommender doesn't make sense."""

    recommender_name: str = Field(min_length=1, max_length=200)
    recommender_role: str = Field(
        min_length=1,
        max_length=200,
        description="The recommender's role/relationship to the applicant, "
        "e.g. 'Academic Advisor', 'Direct Manager'.",
    )


class GeneratedDocumentOut(BaseModel):
    document_type: Literal["cv", "motivation_letter", "recommendation_letter"]
    opportunity_id: UUID
    opportunity_title: str
    content_markdown: str
    generated_at: datetime
