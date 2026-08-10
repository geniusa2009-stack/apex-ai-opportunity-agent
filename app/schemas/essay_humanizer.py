from pydantic import BaseModel, Field


class EssayHumanizeRequest(BaseModel):
    essay_text: str = Field(
        min_length=1,
        # Disclosed judgment call, not in the ask: ~20,000 characters is
        # roughly 3,000-4,000 words -- generous headroom above any real
        # college/scholarship essay's typical ~250-1,000 word length,
        # while still bounding prompt size/cost. Same category of
        # judgment call as `youtube_transcript_max_chars`
        # (app/core/config.py) capping a different unbounded text input.
        max_length=20_000,
        description="The student's original essay text.",
    )
    target_tone: str = Field(
        min_length=1,
        max_length=100,
        description="E.g. 'vulnerable narrative', 'intellectual', 'humorous'.",
    )
