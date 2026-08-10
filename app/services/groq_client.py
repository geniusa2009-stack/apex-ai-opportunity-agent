import groq
from langchain_groq import ChatGroq
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings

# Groq's Python SDK (which langchain-groq calls into) mirrors OpenAI's
# exception naming. Shared by every Groq-backed service in this codebase
# (chatbot.py, skill_gap.py, interview.py) -- extracted here so there's one
# retry policy to tune, not three copies that can drift.
retry_groq_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(
        (groq.APIConnectionError, groq.APITimeoutError, groq.RateLimitError)
    ),
)


def build_chat_groq(temperature: float = 0.3) -> ChatGroq:
    """Fresh `ChatGroq` client per call -- same "no client reuse across
    requests/event-loop contexts" posture used for every other LLM
    provider in this codebase (see app/services/ai.py's docstring for the
    full reasoning: this class gets called from both a persistent FastAPI
    event loop and, potentially, per-task Celery loops, and an
    httpx-backed client isn't safe to assume shareable across those)."""
    return ChatGroq(model=settings.groq_chat_model, api_key=settings.groq_api_key, temperature=temperature)
