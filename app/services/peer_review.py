import logging

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings

logger = logging.getLogger(__name__)

# Same retry posture as the other Gemini-backed services in this
# codebase: `errors.ClientError` (429s) and `errors.ServerError` (5xx)
# both inherit from `errors.APIError`.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)


class PanelReviewResult(BaseModel):
    """Structured output for `simulate_panel_review`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here.

    `final_decision` is a plain `str`, not a hard `Literal` enum -- the
    ask gave "Accept", "Major Revision", "Reject" as examples ("e.g."),
    not an exhaustive list, and a rigid enum would hard-fail validation
    (a 502 to the caller) if the model phrases a decision even slightly
    differently. The system prompt guides it toward the standard
    academic peer-review taxonomy instead (see the note below on the
    added "Minor Revision" category).
    """

    reviewer_1_comment: str = Field(
        description="The Methodologist's comment: research design, sample "
        "size, and data analysis critique."
    )
    reviewer_2_comment: str = Field(
        description="The Innovation Analyst's comment: novelty, literature "
        "gaps, and original contribution critique."
    )
    reviewer_3_comment: str = Field(
        description="The Feasibility Critic's comment: timeline, budget, "
        "and logistical/physical constraint critique."
    )
    panel_debate_summary: str = Field(
        description="A simulated dialogue transcript of the three "
        "reviewers debating each other's points about the proposal -- "
        "formatted as multiple labeled dialogue lines (e.g. 'Reviewer 1: "
        "...\\nReviewer 2: ...'), not a plain summary paragraph."
    )
    final_decision: str = Field(
        description="The panel's consolidated decision -- typically one "
        "of 'Accept', 'Minor Revision', 'Major Revision', or 'Reject' "
        "(standard academic peer-review categories)."
    )
    consolidated_score: int = Field(
        ge=1, le=10, description="1-10 overall panel rating of the proposal. Higher is better."
    )


_PEER_REVIEW_SYSTEM_PROMPT = """\
You are simulating an academic peer-review panel evaluating a student's \
research proposal. The panel has three distinct reviewers, each with a \
specific evaluative focus:

1. Reviewer 1 (The Methodologist): Evaluates research design, sample sizes, \
and data analysis approach. Focused on rigor -- is the methodology sound \
and appropriate for the stated research question?
2. Reviewer 2 (The Innovation Analyst): Evaluates novelty, gaps in the \
existing literature, and the proposal's original contribution. Focused on \
significance -- does this actually add something new?
3. Reviewer 3 (The Feasibility Critic): Evaluates realistic timeline, \
budgeting, and physical/logistical constraints. Focused on practicality -- \
can this actually be done as described, in the time and with the resources \
implied?

You will be given the student's proposal text. Your job:

1. Write each reviewer's individual comment (`reviewer_1_comment`,
`reviewer_2_comment`, `reviewer_3_comment`), fully in their own distinct \
voice and evaluative focus as described above.
2. Write `panel_debate_summary`: a simulated back-and-forth dialogue where \
the three reviewers respond to and debate EACH OTHER's points about this \
specific proposal (not just restate their individual comments in \
sequence) -- e.g. the Innovation Analyst might push back on the \
Methodologist's concern, or the Feasibility Critic might agree with one \
reviewer while disagreeing with another. Format this as labeled dialogue \
lines, one per turn.
3. Reach a `final_decision` the panel would realistically land on given \
the debate, typically one of: Accept, Minor Revision, Major Revision, or \
Reject.
4. Give a `consolidated_score` (1-10) reflecting the panel's overall \
assessment after the debate.

Rules:
- Every critique, in every reviewer's comment AND in the debate, must be \
grounded in what the proposal actually says (or conspicuously fails to \
say) -- do not fabricate a claim, method, or detail the proposal never \
made and then critique that instead.
- Each reviewer must stay in their own lane -- the Methodologist shouldn't \
suddenly critique the budget, nor should the Feasibility Critic suddenly \
critique the literature review -- though they CAN react to each other's \
points in the debate.
- Be genuinely rigorous and realistic, not uniformly harsh or uniformly \
positive -- a strong proposal should be able to earn a good decision and \
score; a weak one should not be inflated.
- The `final_decision` and `consolidated_score` should be consistent with \
each other and with what was actually said in the debate -- do not reach a \
decision that contradicts the substance of the reviewers' own comments.
"""


class PeerReviewGateway:
    """Async wrapper around Gemini-based peer-review panel simulation via
    the `google-genai` SDK -- same package/model-substitution rationale
    as every other Gemini-backed service in this codebase
    (gemini-1.5-flash / google-generativeai are both stale; see the
    README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def simulate_panel_review(self, proposal_text: str) -> PanelReviewResult:
        """Simulate a 3-reviewer academic peer-review panel debating
        `proposal_text`, ending in a consolidated decision and score."""
        client = self._client()

        prompt = f"Student's research proposal:\n{proposal_text}"

        response = await client.aio.models.generate_content(
            model=settings.gemini_peer_review_model,
            contents=[_PEER_REVIEW_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PanelReviewResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for peer-review panel simulation")

        return PanelReviewResult.model_validate_json(response.text)


peer_review_gateway = PeerReviewGateway()
