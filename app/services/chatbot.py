import logging
from collections.abc import Sequence

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import tool
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.groq_client import build_chat_groq, retry_groq_call
from app.services.opportunity_search import SemanticSearchError, semantic_search

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are Apex's assistant, helping users find scholarships, internships, \
volunteer positions, and courses.

Rules:
- Use the `search_opportunities` tool whenever the user is looking for \
specific opportunities (e.g. "medical scholarships in Germany", "remote \
software internships"). Never answer opportunity-finding questions from \
memory or invent listings -- only describe opportunities the tool actually \
returned in this conversation.
- If the tool reports no matches or an error, say so plainly. Do not \
paper over it with a fabricated result.
- Keep responses conversational and reasonably concise. When listing \
opportunities, mention title, type, location, and deadline where known.
- For general conversation unrelated to finding opportunities (greetings, \
questions about the platform itself, etc.), respond normally without \
using the tool.
"""


def _build_search_tool(db: AsyncSession):
    """Build the opportunity-search tool bound to *this* request's DB
    session via closure. Tools constructed with `@tool` are plain
    functions under the hood -- there's no way to hand LangChain a
    per-invocation session through `create_agent`'s API itself, so the
    session is captured by the closure instead, and a fresh tool (and
    fresh agent, see `chat()` below) is built for every request."""

    @tool
    async def search_opportunities(query: str, limit: int = 5) -> str:
        """Search Apex's database of verified scholarships, internships,
        volunteer positions, and courses using natural-language semantic
        search. Use this whenever the user asks to find, search for,
        browse, or learn about specific opportunities. Do not use this
        for general conversation or questions unrelated to finding
        opportunities."""
        try:
            results = await semantic_search(db, query, limit=limit)
        except SemanticSearchError:
            return (
                "Opportunity search is temporarily unavailable (embedding "
                "service error). Tell the user search isn't working right "
                "now rather than making up results."
            )

        if not results:
            return (
                f"No verified opportunities matched {query!r}. Tell the "
                "user no matches were found and suggest a broader or "
                "differently-worded query -- do not invent results."
            )

        lines = [f"Found {len(results)} matching verified opportunities:"]
        for r in results:
            deadline = r.deadline.date().isoformat() if r.deadline else "no stated deadline"
            location = r.country or "location unspecified"
            lines.append(
                f'- "{r.title}" ({r.type.value}, {location}, deadline: '
                f"{deadline}, similarity: {r.similarity_score}) -- {r.target_url}"
            )
        return "\n".join(lines)

    return search_opportunities


@retry_groq_call
async def _run_agent(messages: list[BaseMessage], search_tool) -> str:
    llm = build_chat_groq(temperature=0.3)
    agent = create_agent(model=llm, tools=[search_tool], system_prompt=_SYSTEM_PROMPT)

    result = await agent.ainvoke({"messages": messages})
    final_message = result["messages"][-1]
    return final_message.content


async def chat(message: str, history: Sequence[tuple[str, str]], db: AsyncSession) -> str:
    """Run one turn of the Apex chatbot and return its text reply.

    A fresh LLM client, search tool, and agent graph are built on every
    call rather than reused from a module-level singleton -- unlike the
    stateless `ai_gateway`/`vision_gateway` singletons elsewhere in this
    codebase, the search tool here must be bound to *this request's*
    AsyncSession (DB sessions are request-scoped, not safe to share
    across requests). Compiling a LangGraph agent graph is in-memory
    graph construction with no network I/O -- overhead here is negligible
    next to the LLM call itself.

    `history` is a plain sequence of (role, content) tuples -- `"user"` or
    `"assistant"` -- not a Pydantic schema type, so this service stays
    decoupled from the API layer's request/response shapes, matching
    `AIGateway`'s design (app/services/ai.py). There's no server-side
    conversation storage: the caller (the /chat endpoint) is responsible
    for resending prior turns each request.
    """
    messages: list[BaseMessage] = []
    for role, content in history:
        messages.append(HumanMessage(content=content) if role == "user" else AIMessage(content=content))
    messages.append(HumanMessage(content=message))

    search_tool = _build_search_tool(db)
    return await _run_agent(messages, search_tool)
