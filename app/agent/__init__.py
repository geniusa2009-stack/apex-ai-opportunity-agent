"""APEX AI Opportunity Agent.

A modular-monolith package that turns raw opportunity messages (WhatsApp,
volunteers, Telegram, websites, RSS) into reviewed, human-approved APEX
posts. It plugs into the existing backend instead of replacing it:

    connectors/   -> unified IngestionEvent (app/agent/ingestion.py)
    ingestion     -> SourceMessage + PipelineJob rows
    normalization -> deterministic URL/text/title normalization + hashes
    dedup         -> deterministic layers; embeddings only propose candidates
    llm/          -> provider-agnostic structured generation + self-correction
    extraction    -> ExtractedOpportunity (Pydantic-validated)
    verification  -> reuses the existing D6 Claim/Evidence system
    writer        -> APEX post (template or LLM + fact guard)
    review        -> deterministic quality gate (+ optional LLM review)
    approval      -> human approve / reject / request revision
    publishing    -> PublisherConnector adapters + idempotent Publication rows
    rag           -> historical examples ranked by similarity/performance/recency
    performance   -> PostPerformance measurements
    jobs          -> persistent, lease-based job runner with retries/dead letter
    pipeline      -> orchestrates the stages for one job

See ARCHITECTURE.md and PIPELINE.md at the repository root.
"""
