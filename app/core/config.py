from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central app configuration, loaded from environment variables / .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    environment: str = "development"
    secret_key: str = "change_me_in_production"
    api_v1_prefix: str = "/api/v1"
    project_name: str = "Apex"

    # Auth (Step 3.2 originally; superseded by Supabase JWT verification
    # below as of the Supabase integration step). `jwt_algorithm` stays
    # HS256 -- that's also the algorithm Supabase signs its access
    # tokens with, so this setting is reused rather than duplicated.
    # `access_token_expire_minutes` is now unused dead config (it
    # governed the local `create_access_token`, which no longer exists)
    # -- left in place rather than removed, since deleting a config
    # field is a slightly riskier/noisier change than leaving an inert
    # one, and it costs nothing to keep.
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # Supabase JWT verification (Supabase integration step). This is
    # the "JWT Secret" from Supabase's project API settings -- used to
    # verify the signature of tokens Supabase itself issues, entirely
    # locally (no network round-trip to Supabase needed to validate a
    # token). Required -- there's no sane default for a real secret.
    supabase_jwt_secret: str

    # Database. `database_url` now points at Supabase's Postgres
    # connection string (either the direct connection on port 5432, or
    # the pgbouncer transaction-mode pooler on port 6543 -- see the
    # comment in app/db/session.py on why that choice matters for how
    # SQLAlchemy/asyncpg needs to be configured).
    database_url: str
    sync_database_url: str

    # Redis / Celery
    redis_url: str
    celery_broker_url: str
    celery_result_backend: str

    # AI providers (used starting Phase 2)
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    ai_verification_model: str = "gpt-4o-mini"
    ai_embedding_model: str = "text-embedding-3-small"
    ai_confidence_threshold: float = 0.7

    # Anthropic: document generation (Step 3.3). Model IDs move on -- verify
    # this against Anthropic's current model list rather than assuming it's
    # still accurate by the time you read this.
    anthropic_document_model: str = "claude-sonnet-5"
    anthropic_max_tokens: int = 2048
    anthropic_document_temperature: float = 0.7

    # Gemini vision/document extraction (Step 3.4), via the `google-genai`
    # package. Model name and free-tier terms both change often -- verify
    # against https://ai.google.dev/gemini-api/docs/pricing before relying
    # on this rather than assuming it's still accurate or still free.
    gemini_api_key: str | None = None
    gemini_vision_model: str = "gemini-2.5-flash"

    # Gemini text grading (Step 3.8 / Feature 4: essay/motivation-letter
    # grader). Same underlying model as gemini_vision_model today, but kept
    # as its own setting so the two use cases can be tuned/swapped
    # independently. gemini-1.5-flash (the model literally named in the
    # ask) has been superseded by gemini-2.5-flash -- see the README note
    # on this from Step 3.4, which already covered this exact substitution.
    gemini_grading_model: str = "gemini-2.5-flash"

    # Gemini structured verification (Phase 2: Real Verification Provider
    # Diversity). A genuine second, independently-callable provider for
    # `ReasoningTaskType.VERIFICATION` alongside OpenAI's
    # `ai_verification_model` -- kept as its own setting (not reused from
    # `gemini_grading_model`) so this specific, structured-output-bearing
    # call can be tuned/swapped independently of grading, per the same
    # independent-tuning rationale as every other `gemini_*_model` setting.
    gemini_verification_model: str = "gemini-2.5-flash"

    # Gemini roadmap planning (Step 3.9 / Feature 5). Same model default
    # as the other Gemini features again, on its own setting for the same
    # independent-tuning reason -- see the gemini_grading_model note above.
    gemini_planner_model: str = "gemini-2.5-flash"

    # Gemini auto-tagging (Step 3.10 / Feature 6). Same model default and
    # independent-tuning rationale as the other Gemini features above.
    gemini_tagger_model: str = "gemini-2.5-flash"

    # Gemini grammar/tone polishing (Step 3.11 / Feature 7). Same model
    # default and independent-tuning rationale as the other Gemini
    # features above.
    gemini_polisher_model: str = "gemini-2.5-flash"

    # Gemini deadline extraction (Step 3.14 / Feature 10). Same model
    # default and independent-tuning rationale as the other Gemini
    # features above.
    gemini_extractor_model: str = "gemini-2.5-flash"

    # Gemini professor outreach email drafting (Step 3.15 / Advanced
    # Feature 1). Same model default and independent-tuning rationale as
    # the other Gemini features above.
    gemini_professor_model: str = "gemini-2.5-flash"

    # Gemini portfolio website generation (Step 3.17 / Advanced Feature
    # 3). Same model default and independent-tuning rationale as the
    # other Gemini features above.
    gemini_portfolio_model: str = "gemini-2.5-flash"

    # Gemini document authenticity checking (Step 3.18 / Advanced
    # Feature 4), vision-capable like gemini_vision_model (Step 3.4) but
    # kept as its own setting -- same independent-tuning rationale as
    # every other Gemini feature above.
    gemini_document_checker_model: str = "gemini-2.5-flash"

    # Gemini YouTube video insights extraction (Step 3.19 / Advanced
    # Feature 5). Same model default and independent-tuning rationale as
    # every other Gemini feature above.
    gemini_youtube_model: str = "gemini-2.5-flash"

    # Gemini auto-fill schema generation (Step 3.20 / Advanced Feature
    # 6). Same model default and independent-tuning rationale as every
    # other Gemini feature above.
    gemini_autofill_model: str = "gemini-2.5-flash"

    # Gemini devil's-advocate proposal criticism (Step 3.21 / Advanced
    # Feature 7). Same model default and independent-tuning rationale as
    # every other Gemini feature above.
    gemini_devils_advocate_model: str = "gemini-2.5-flash"

    # Gemini alumni matchmaking (Step 3.22 / Advanced Feature 8). Same
    # model default and independent-tuning rationale as every other
    # Gemini feature above.
    gemini_alumni_matchmaker_model: str = "gemini-2.5-flash"

    # Gemini acceptance probability engine (Step 3.23 / Advanced Feature
    # 9). Same model default and independent-tuning rationale as every
    # other Gemini feature above.
    gemini_probability_model: str = "gemini-2.5-flash"

    # Gemini live web opportunity explorer (Step 3.24 / Advanced Feature
    # 10). Same model default and independent-tuning rationale as every
    # other Gemini feature above.
    gemini_web_explorer_model: str = "gemini-2.5-flash"

    # Live web search (Step 3.24). How many raw DuckDuckGo results to
    # fetch per query before summarizing -- kept modest since each raw
    # result is a snippet fed into the Gemini prompt, not a full page.
    web_explorer_max_results: int = 8

    # Gemini cultural context localization (Step 3.26 / Legendary Feature
    # 2). Same model default and independent-tuning rationale as every
    # other Gemini feature above.
    gemini_cultural_localizer_model: str = "gemini-2.5-flash"

    # Gemini autonomous scholar agent (Step 3.27 / Legendary Feature 3).
    # Same model default and independent-tuning rationale as every other
    # Gemini feature above.
    gemini_scholar_agent_model: str = "gemini-2.5-flash"

    # Gemini peer-review panel simulation (Step 3.28 / Legendary Feature
    # 4). Same model default and independent-tuning rationale as every
    # other Gemini feature above.
    gemini_peer_review_model: str = "gemini-2.5-flash"

    # Gemini PDF form-field mapping (Step 3.29 / Legendary Feature 5).
    # Same model default and independent-tuning rationale as every other
    # Gemini feature above.
    gemini_pdf_filler_model: str = "gemini-2.5-flash"

    # Gemini scholarship trend prediction (Step 3.30 / Legendary Feature
    # 6). Same model default and independent-tuning rationale as every
    # other Gemini feature above.
    gemini_trend_predictor_model: str = "gemini-2.5-flash"

    # Gemini recommendation letter writing (Step 3.31 / Legendary Feature
    # 7). Same model default and independent-tuning rationale as every
    # other Gemini feature above.
    gemini_recommendation_writer_model: str = "gemini-2.5-flash"

    # Gemini gamified quest planning (Step 3.32 / Legendary Feature 8).
    # Same model default and independent-tuning rationale as every other
    # Gemini feature above.
    gemini_quest_planner_model: str = "gemini-2.5-flash"

    # Gemini rejection appeal drafting (Step 3.33 / Legendary Feature 9).
    # Same model default and independent-tuning rationale as every other
    # Gemini feature above.
    gemini_appeal_drafter_model: str = "gemini-2.5-flash"

    # Gemini voice scholar chat reply (Step 3.34 / Legendary Feature
    # 10). Same model default and independent-tuning rationale as every
    # other Gemini feature above. No separate Groq setting needed for
    # this feature -- STT reuses groq_whisper_model via voice_coach.py's
    # existing transcribe_audio.
    gemini_voice_scholar_model: str = "gemini-2.5-flash"

    # Gemini extracurricular architect (Step 3.35 / High School Feature
    # 1). Same model default and independent-tuning rationale as every
    # other Gemini feature above.
    gemini_extracurricular_model: str = "gemini-2.5-flash"

    # Gemini olympiad/competition scout (Step 3.36 / High School Feature
    # 2). Same model default and independent-tuning rationale as every
    # other Gemini feature above.
    gemini_competition_scout_model: str = "gemini-2.5-flash"

    # Gemini summer program matchmaker (Step 3.37 / High School Feature
    # 3). Same model default and independent-tuning rationale as every
    # other Gemini feature above.
    gemini_summer_program_model: str = "gemini-2.5-flash"

    # Gemini standardized test pathway planner (Step 3.38 / High School
    # Feature 4). Same model default and independent-tuning rationale as
    # every other Gemini feature above.
    gemini_test_pathway_model: str = "gemini-2.5-flash"

    # Gemini STEM school admission coach (Step 3.39 / High School
    # Feature 5). Same model default and independent-tuning rationale as
    # every other Gemini feature above.
    gemini_stem_coach_model: str = "gemini-2.5-flash"

    # Gemini cultural exchange program advisor (Step 3.40 / High School
    # Feature 6). Same model default and independent-tuning rationale as
    # every other Gemini feature above.
    gemini_exchange_advisor_model: str = "gemini-2.5-flash"

    # Gemini UWC scholarship strategist (Step 3.41 / High School Feature
    # 7). Same model default and independent-tuning rationale as every
    # other Gemini feature above.
    gemini_uwc_strategist_model: str = "gemini-2.5-flash"

    # Gemini high school resume builder (Step 3.42 / High School Feature
    # 8). Same model default and independent-tuning rationale as every
    # other Gemini feature above.
    gemini_high_school_resume_model: str = "gemini-2.5-flash"

    # Gemini Common App essay idea sparker (Step 3.43 / High School
    # Feature 9). Same model default and independent-tuning rationale as
    # every other Gemini feature above.
    gemini_essay_sparker_model: str = "gemini-2.5-flash"

    # Gemini school teacher recommendation guide (Step 3.44 / High
    # School Feature 10). Same model default and independent-tuning
    # rationale as every other Gemini feature above.
    gemini_teacher_guide_model: str = "gemini-2.5-flash"

    # Gemini international grade converter (Step 3.45 / High School
    # Feature 11). Same model default and independent-tuning rationale
    # as every other Gemini feature above.
    gemini_grade_converter_model: str = "gemini-2.5-flash"

    # Gemini parent consent & financial guide (Step 3.46 / High School
    # Feature 12). Same model default and independent-tuning rationale
    # as every other Gemini feature above.
    gemini_parent_guide_model: str = "gemini-2.5-flash"

    # Gemini fully-funded safety filter (Step 3.47 / High School Feature
    # 13). Same model default and independent-tuning rationale as every
    # other Gemini feature above.
    gemini_safety_filter_model: str = "gemini-2.5-flash"

    # Gemini high school research mentor (Step 3.48 / High School
    # Feature 14). Same model default and independent-tuning rationale
    # as every other Gemini feature above.
    gemini_research_mentor_model: str = "gemini-2.5-flash"

    # Gemini teen portfolio curator (Step 3.49 / High School Feature
    # 15). Same model default and independent-tuning rationale as every
    # other Gemini feature above.
    gemini_portfolio_curator_model: str = "gemini-2.5-flash"

    # Gemini friendly teen interview simulator (Step 3.50 / High School
    # Feature 16). Same model default and independent-tuning rationale
    # as every other Gemini feature above.
    gemini_teen_interview_model: str = "gemini-2.5-flash"

    # Gemini academic culture shock evaluator (Step 3.51 / High School
    # Feature 17). Same model default and independent-tuning rationale
    # as every other Gemini feature above.
    gemini_culture_shock_model: str = "gemini-2.5-flash"

    # Gemini undergrad scholarship targeter (Step 3.52 / High School
    # Feature 18). Same model default and independent-tuning rationale
    # as every other Gemini feature above.
    gemini_undergrad_targeter_model: str = "gemini-2.5-flash"

    # Gemini safe teen-alumni connector (Step 3.53 / High School Feature
    # 19). Same model default and independent-tuning rationale as every
    # other Gemini feature above.
    gemini_alumni_connector_model: str = "gemini-2.5-flash"

    # Gemini scholarship rejection recovery plan (Step 3.54 / High
    # School Feature 20, final feature of this tier). Same model default
    # and independent-tuning rationale as every other Gemini feature
    # above.
    gemini_scholarship_recovery_model: str = "gemini-2.5-flash"

    # YouTube transcript fetching (Step 3.19). Max characters of
    # transcript text sent to Gemini per call -- long videos (2+ hours)
    # can produce transcripts well beyond what's useful or cost-
    # effective to send in one prompt; truncating to the first ~60k
    # characters (roughly 1.5-2 hours of typical speech) keeps this
    # bounded without needing chunking/map-reduce summarization yet.
    youtube_transcript_max_chars: int = 60_000

    # Groq (Step 3.5 chatbot). Groq's free-tier model lineup has churned
    # hard: llama3-8b-8192 (deprecated 2025-05-31) -> llama-3.1-8b-instant
    # (deprecated 2026-06-17) -> Groq's own recommended replacement is
    # openai/gpt-oss-20b, an open-weight model, which is what this
    # defaults to. Verify against https://console.groq.com/docs/models
    # before assuming any specific model name here is still live.
    groq_api_key: str | None = None
    groq_chat_model: str = "openai/gpt-oss-20b"

    # Groq Whisper speech-to-text (Step 3.16 / Advanced Feature 2: Voice
    # Interview Coach). Verified via Groq's docs as of this build:
    # whisper-large-v3 is NOT deprecated (unlike the chat-model churn
    # above) -- whisper-large-v3-turbo exists as a faster alternative,
    # and distil-whisper-large-v3-en is what's actually being phased out.
    # Free tier accepts audio files up to 25MB. Verify against
    # https://console.groq.com/docs/speech-to-text before relying on
    # either fact staying true.
    groq_whisper_model: str = "whisper-large-v3"

    # Semantic search (app/api/v1/endpoints/opportunities.py). Cosine
    # similarity for real text embeddings clusters lower than intuition
    # suggests -- 0.5 is a reasonable starting default, not a validated
    # number. Expect to tune this empirically once there's real query/result
    # data to look at; callers can also override it per-request.
    search_similarity_threshold: float = 0.5

    # --- Enterprise-Grade AI Memory & Cloud Vault System integration
    # step (no step number -- an infrastructure step, like the Supabase
    # DB/JWT integration before it) ---

    # Supabase Storage. `supabase_url` is the project's REST/Storage API
    # base (`https://<project>.supabase.co`), distinct from `database_url`
    # above -- Storage talks to Supabase's HTTP API, not directly to
    # Postgres. `supabase_service_role_key` (NOT the anon/public key) is
    # used because every Storage write here happens on the backend's own
    # authority after it has already verified the caller via
    # `get_current_user` -- same trust model this backend already uses
    # for Postgres (the backend writes directly with its own DB
    # credentials, not per-user RLS-scoped ones; see
    # app/services/storage_service.py's class docstring). Keep this key
    # server-side only -- it bypasses Storage's Row Level Security
    # entirely.
    supabase_url: str
    supabase_service_role_key: str

    # Gemini text embeddings for StudentMemory (app/models/student_memory.py).
    # `text-embedding-004` per this step's explicit instruction -- NOT the
    # same model or dimensionality as `ai_embedding_model` above (OpenAI's
    # text-embedding-3-small, 1536-dim, used for the public Opportunity
    # catalog's search/dedup). This one is 768-dim and powers each
    # student's own private semantic memory search instead. See
    # app/services/memory_service.py.
    gemini_embedding_model: str = "text-embedding-004"

    # Gemini artifact-summarization call (app/services/memory_service.py's
    # `summarize_artifact_for_memory`) -- necessary plumbing not literally
    # named in the ask: `generate_and_store_embedding` takes
    # already-summarized `text_to_embed`, but something has to produce
    # that summary text from a raw artifact JSON payload first. Kept as
    # its own setting, same independent-tuning rationale as every other
    # Gemini feature setting in this file.
    gemini_memory_summarizer_model: str = "gemini-2.5-flash"

    # --- Ecosystem Feature 1: AI-Powered Application Kanban Tracker ---

    # Gemini scholarship-preparation-checklist generation
    # (app/services/application_tracker.py). The ask literally named
    # `gemini-1.5-flash` -- same substitution already made (and disclosed)
    # for every Gemini feature since Step 3.4: `gemini-1.5-flash` has been
    # superseded by `gemini-2.5-flash`, so that's what this defaults to
    # instead. Kept as its own setting, same independent-tuning rationale
    # as every other Gemini feature setting in this file.
    gemini_tracker_model: str = "gemini-2.5-flash"

    # --- Ecosystem Feature 2: AI Smart Deadline Alerts & Calendar Sync ---

    # Gemini backward-planning milestone generation
    # (app/services/deadline_alerts.py). Ask literally named
    # `gemini-1.5-flash` -- same established substitution as
    # `gemini_tracker_model` above (superseded by gemini-2.5-flash).
    # Kept as its own setting, same independent-tuning rationale as every
    # other Gemini feature setting in this file.
    gemini_deadline_alerts_model: str = "gemini-2.5-flash"

    # --- Ecosystem Feature 3: AI Detector Safeguard & Essay Humanizer ---
    # Gemini essay analysis + rewrite (app/services/essay_humanizer.py).
    # Unlike the two preceding Ecosystem features, this ask didn't name a
    # specific (stale) model version to substitute -- defaults straight
    # to gemini-2.5-flash, same independent-tuning rationale as every
    # other Gemini feature setting in this file.
    gemini_essay_humanizer_model: str = "gemini-2.5-flash"

    # --- Ecosystem Feature 4: AI Accountability Coach & Premium Token
    # Economy --- Gemini empathetic pep-talk generation
    # (app/services/coach_service.py). Ask literally named
    # `gemini-1.5-flash` -- same established substitution as every other
    # Gemini feature since Step 3.4 (superseded by gemini-2.5-flash).
    gemini_coach_model: str = "gemini-2.5-flash"

    # --- Ecosystem Feature 5 (Upgraded): AI Multi-Sensory Study Buddy &
    # Distraction Shield --- Gemini soundscape/brain-break suggestion,
    # active-recall quiz generation, and quiz-answer evaluation (all
    # three in app/services/focus_service.py). This ask literally named
    # `gemini-1.5-flash` for `generate_active_recall_quiz` specifically --
    # same established substitution as every other Gemini feature since
    # Step 3.4 (superseded by gemini-2.5-flash). One shared setting for
    # all three of this file's Gemini calls (rather than one per method)
    # since all three are the same call "shape" (structured JSON text
    # generation) for one feature, matching how single-purpose Gemini
    # features elsewhere in this file get exactly one setting regardless
    # of how many internal calls they make.
    gemini_focus_model: str = "gemini-2.5-flash"

    # --- Ecosystem Feature 6 (Upgraded): AI Knowledge Realms, Anonymous
    # Avatars & Guild Quests --- Gemini safety/PII moderation, anonymous
    # alias generation, suggested-rewrite evaluation, and guild quest
    # generation (all in app/services/guild_service.py). This ask
    # literally named `gemini-1.5-flash` for the alias-generation call --
    # same established substitution as every other Gemini feature since
    # Step 3.4 (superseded by gemini-2.5-flash). One shared setting for
    # all four of this file's Gemini calls, same "one setting per
    # feature, not per call" rationale as `gemini_focus_model` above.
    gemini_guilds_model: str = "gemini-2.5-flash"

    # --- Ecosystem Feature 7: AI Parent Pitcher & Reassurance Portal ---
    # Gemini persuasion-strategy + formal Arabic letter generation
    # (app/services/parent_pitch_service.py). Ask literally named
    # `gemini-1.5-flash` -- same established substitution as every other
    # Gemini feature since Step 3.4 (superseded by gemini-2.5-flash).
    gemini_parent_pitch_model: str = "gemini-2.5-flash"

    # --- Ecosystem Feature 8 (Upgraded): AI Multidimensional Path
    # Simulator & Reality Checker --- Gemini weighted-scoring + future-
    # simulation + reality-check + verdict generation
    # (app/services/decision_service.py). Ask literally named
    # `gemini-1.5-flash` -- same established substitution as every other
    # Gemini feature since Step 3.4 (superseded by gemini-2.5-flash).
    gemini_decision_matrix_model: str = "gemini-2.5-flash"

    # --- Ecosystem Feature 9: AI Smart Budget & Cost of Living Simulator ---
    # Gemini cost-of-living simulation + legal student-job research
    # (app/services/budget_service.py). Ask literally named
    # `gemini-1.5-flash` -- same established substitution as every other
    # Gemini feature since Step 3.4 (superseded by gemini-2.5-flash).
    gemini_budget_model: str = "gemini-2.5-flash"

    # --- Ecosystem Feature 10 (Upgraded): AI Expat RPG & Cultural
    # Landmine Game --- Gemini slang-dictionary + scenario generation,
    # turn continuation, and landmine detection
    # (app/services/expat_survival_service.py). Ask literally named
    # `gemini-1.5-flash` -- same established substitution as every other
    # Gemini feature since Step 3.4 (superseded by gemini-2.5-flash).
    gemini_expat_survival_model: str = "gemini-2.5-flash"

    # --- Ecosystem Features 11 & 12: AI-Powered Social Hub, Share Cards
    # & Study Duels --- Gemini shareable "Social Card" generation
    # (title, theme, motivating quote, color palette) in
    # app/services/social_service.py. Ask literally named
    # `gemini-1.5-flash` -- same established substitution as every other
    # Gemini feature setting in this file (superseded by
    # gemini-2.5-flash; see the comment on this setting in
    # app/core/config.py).
    gemini_social_model: str = "gemini-2.5-flash"

    # --- Ecosystem Features 13 & 14: AI-Powered LMS & Interactive
    # Academy --- Gemini course-copilot Q&A and lesson-quiz generation
    # (both in app/services/course_service.py). Ask literally named
    # `gemini-1.5-flash` -- same established substitution as every other
    # Gemini feature setting in this file (superseded by
    # gemini-2.5-flash; see the comment on this setting in
    # app/core/config.py). One shared setting for both of this file's
    # Gemini calls, same "one setting per feature, not per call"
    # rationale as `gemini_focus_model`/`gemini_guilds_model`.
    gemini_courses_model: str = "gemini-2.5-flash"

    # --- Core Video Media Processing Pipeline & Intelligent
    # Recommendation Engine (built on top of Ecosystem Features 13 & 14)
    # ---

    # Pinata IPFS pinning (app/services/media_service.py). Modern JWT
    # auth (`Authorization: Bearer <PINATA_JWT>`), not the legacy
    # `pinata_api_key`/`pinata_secret_api_key` header pair -- per
    # Pinata's own current documentation as of this build; verify
    # against https://docs.pinata.cloud before assuming this is still
    # their recommended method. `str | None` (not required) since this
    # entire pipeline is inert -- but the rest of the app still boots
    # fine -- without a real Pinata account configured, same posture as
    # `gemini_api_key`/`groq_api_key` above.
    pinata_jwt: str | None = None

    # Public gateway used to build a playable manifest URL from a stored
    # root CID (`{pinata_gateway_url}/{cid}/master.m3u8}`) in
    # `GET /lessons/{lesson_id}/stream-manifest`'s response. Pinata's own
    # shared public gateway by default; a project with a dedicated
    # Pinata gateway domain would override this for better performance/
    # reliability, but the shared gateway works for any CID regardless
    # of which account pinned it.
    pinata_gateway_url: str = "https://gateway.pinata.cloud/ipfs"

    # AI Recommendation Engine (app/services/recommendation_service.py).
    # Cosine DISTANCE cutoff (not similarity -- pgvector's `<=>` operator
    # returns `1 - cosine_similarity`, so smaller is more relevant)
    # beyond which a course is considered too dissimilar to a student's
    # interest vector to bother recommending at all. Same "reasonable
    # starting default, not a validated number, expect to tune
    # empirically once there's real embedding data to look at" posture
    # already disclosed for `search_similarity_threshold` above -- this
    # is that same kind of setting, for a different embedding space
    # (Gemini text-embedding-004, 768-dim, shared with
    # `StudentMemory.embedding`/`Course.embedding`) and a distance
    # (rather than similarity) convention.
    # Course embeddings reuse `gemini_embedding_model` above directly
    # (same model, same 768-dim space as `StudentMemory.embedding`) --
    # deliberately NOT a separate setting, since two different values
    # here would silently break every cosine-distance comparison this
    # engine depends on (see `Course.embedding`'s column comment in
    # app/models/courses.py).
    recommendation_max_distance: float = 0.8

    # --- Student Video Profiles & AI-Moderated Upload System (built on
    # top of the Core Video Media Processing Pipeline above). ORIGINALLY
    # reused `pinata_jwt`/`pinata_gateway_url` directly for its own
    # HLS+IPFS sub-step -- no longer true as of the Student Video Pinata
    # -> Cloudflare R2 Migration step below, which moved this pipeline's
    # HLS output AND thumbnails off Pinata entirely onto Cloudflare R2.
    # `Course`/`Lesson` (media_service.py, media_router.py) still use
    # `pinata_jwt`/`pinata_gateway_url` exactly as before -- that pipeline
    # was explicitly out of scope for the R2 migration. See the new `r2_*`
    # settings block below. ---

    # Gemini safety/quality evaluation of a transcribed student video
    # (app/services/student_video_service.py). Ask literally named
    # `gemini-1.5-flash` -- same established substitution as every other
    # Gemini feature setting in this file (superseded by
    # gemini-2.5-flash; see the comment on `gemini_grading_model` above,
    # the first place this substitution was made and disclosed).
    gemini_student_video_model: str = "gemini-2.5-flash"

    # No new OpenAI settings needed for Whisper transcription or the
    # summary embedding -- both reuse `openai_api_key` above directly
    # (Whisper's `whisper-1` model is hardcoded in
    # `student_video_service.py`, matching how `groq_whisper_model` is
    # the only STT model setting this codebase has ever needed one for;
    # a second one for a different provider's fixed model name would be
    # redundant). The summary embedding reuses `ai_embedding_model`
    # (`text-embedding-3-small`) AND `search_similarity_threshold`
    # directly too -- `StudentVideo.summary_embedding` is deliberately
    # the SAME 1536-dim OpenAI embedding space as `Opportunity.embedding`
    # (see that column's comment in app/models/student_videos.py), so the
    # existing similarity threshold tuned for that space is valid to
    # reuse here rather than needing its own separately-tuned setting.

    # --- Student Video Pinata -> Cloudflare R2 Migration ---
    #
    # Disclosed cost/architecture rationale (why this migration exists at
    # all, for the record): Pinata's free tier and IPFS gateway egress
    # both scale poorly once a platform is actually serving many students'
    # worth of HLS segment requests -- Cloudflare R2's S3-compatible API,
    # zero egress fees, and free Range-request support (required for HLS
    # seek) make it the right backend for this pipeline's actual traffic
    # pattern. Scope is deliberately narrow: ONLY the Student Video
    # pipeline (`student_video_service.py`/`tasks/student_video.py`)
    # moved -- the Core Video Media Processing Pipeline's admin-authored
    # Course/Lesson videos stay on Pinata/IPFS, a disclosed, explicit
    # product decision (not every video pipeline in this codebase needed
    # to migrate at once).
    #
    # All five settings are `str | None` (not required), same posture as
    # `pinata_jwt` -- this pipeline is inert without real R2 credentials
    # configured, but the rest of the app still boots fine.

    # R2 account id -- the first path segment of R2's S3-compatible
    # endpoint (`https://{r2_account_id}.r2.cloudflarestorage.com`).
    # Found on the Cloudflare dashboard's R2 overview page, NOT the same
    # value as any bucket name or API token.
    r2_account_id: str | None = None

    # R2 API token's Access Key ID / Secret Access Key pair -- generated
    # from the Cloudflare dashboard (R2 -> "Manage R2 API Tokens"), used
    # as this boto3 S3 client's `aws_access_key_id`/`aws_secret_access_key`
    # (R2's S3-compatible API accepts standard AWS SigV4 auth under these
    # same parameter names -- no Cloudflare-specific auth scheme).
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None

    # The one bucket this pipeline's HLS output + thumbnails are written
    # to. Not created by this code -- provisioned once via the Cloudflare
    # dashboard/`wrangler` CLI, same "this app assumes the bucket already
    # exists" posture as `storage_service.py`'s Supabase Storage buckets.
    r2_bucket_name: str | None = None

    # Public base URL this bucket's objects are actually reachable at --
    # EITHER R2's own free `https://pub-<hash>.r2.dev` public-bucket URL
    # (enabled per-bucket via the dashboard's "Public access" toggle) OR a
    # custom domain mapped to the bucket via Cloudflare's own DNS/Workers
    # routing. `r2_service.py` does not derive this from `r2_account_id`
    # itself -- the R2 S3-compatible ENDPOINT
    # (`{account_id}.r2.cloudflarestorage.com`, used for the authenticated
    # boto3 PUT calls) and the bucket's PUBLIC read URL are two genuinely
    # different hostnames in R2's own architecture; the endpoint is not
    # itself publicly readable without a signed request. `str | None`
    # (not required) for the same "app still boots without it" reason as
    # every other setting here, but a real value MUST be configured before
    # this pipeline can produce a URL any browser could actually play --
    # disclosed limitation, see the README.
    r2_public_base_url: str | None = None

    # --- Hybrid Opportunity Aggregation Engine ---
    #
    # Gemini structured-extraction call (`app/services/aggregator_service.py`'s
    # `process_raw_post_via_ai`) -- same "gemini-2.5-flash" default every
    # other Gemini feature setting in this file uses; reuses the existing
    # `gemini_api_key` setting above rather than adding a second one (one
    # API key already covers every Gemini feature in this codebase).
    gemini_opportunity_aggregator_model: str = "gemini-2.5-flash"

    # Path to the JSON config file listing this pipeline's Telegram
    # channels / Facebook pages (`app/data/opportunity_sources.json`),
    # relative to the backend project root. Disclosed addition: this
    # step's own ask describes loading "a JSON config file" but doesn't
    # name a settings field for its path -- without one, the path would
    # be a hardcoded magic string inside `aggregator_service.py` with no
    # way to point at a different file (e.g. a staging config) without
    # editing code. `aggregator_service._resolve_config_path` anchors
    # this relative path on `Path(__file__)` rather than the process's
    # current working directory, so it resolves correctly regardless of
    # which directory a Celery worker/FastAPI process happens to be
    # started from.
    opportunity_sources_config_path: str = "app/data/opportunity_sources.json"

    # --- Phase 3B: Real Source Adapters & Live Discovery Execution ---
    #
    # `LiveTelegramSourceAdapter` (app/services/telegram_live_client.py)
    # fetches a channel's real, public `https://t.me/s/<handle>` preview
    # page -- no credentials needed, no auth bypass (a public page
    # Telegram itself serves). These three settings are the SAME kind of
    # "bounded execution" control D4.2's own `_HARD_ROUND_CEILING` is for
    # the round loop -- here scoped to one adapter's one fetch call, so a
    # single misbehaving channel (an unbounded post history) can never
    # turn into an unbounded network loop regardless of what D4.2 asks
    # for. Real per-request/connect/read timeouts, never "wait forever."
    telegram_live_fetch_timeout_seconds: float = 10.0
    telegram_live_fetch_max_pages: int = 3
    telegram_live_fetch_max_items: int = 60

    # `FacebookGraphAPISourceAdapter` (app/services/facebook_graph_client.py):
    # empty-by-default, same honest-availability convention as
    # `research_orchestrator._provider_available`'s AI-provider keys --
    # in this dev sandbox this is empty, so that adapter is honestly
    # UNCONFIGURED (see that module's own docstring) rather than silently
    # falling back to fabricated/mocked data.
    facebook_graph_api_access_token: str = ""

    # --- Smart Notifications Engine ---
    #
    # Transactional email dispatch (`app/workers/notification_tasks.py`'s
    # `send_transactional_email_task`). This step's own ask offers a
    # choice: "using a mock sender or basic smtplib setup." Both exist
    # here, chosen at RUNTIME rather than hardcoded to one or the other:
    # `smtp_host` unset (`None`, the default) -- the same "inert until
    # configured" posture as `pinata_jwt`/`r2_account_id` elsewhere in
    # this file -- makes the task run in MOCK mode (logs the would-be
    # email at INFO level and returns, no real network I/O, no SMTP
    # server required to exercise this codebase's own tests or a fresh
    # local checkout). Setting all four `smtp_*` values switches it to a
    # real `smtplib.SMTP_SSL` send. No new dependency needed either way
    # -- `smtplib` is Python's own standard library.
    smtp_host: str | None = None
    smtp_port: int = 465
    smtp_username: str | None = None
    smtp_password: str | None = None
    # The `From:` address on outgoing mail. Deliberately its OWN setting
    # rather than reusing `smtp_username` -- many real SMTP providers
    # (e.g. SendGrid, Postmark) authenticate with an API-key-shaped
    # `smtp_username` that is NOT itself a deliverable-from address.
    smtp_from_email: str | None = None

    # --- Live 1:1 Video Sessions ---
    #
    # ICE server list handed to the frontend's `RTCPeerConnection` config
    # (`GET /live-sessions/ice-config`) -- a STUN server, NOT a TURN
    # server. STUN (used here) only helps two peers DISCOVER their own
    # public IP:port for a direct connection attempt; it costs nothing to
    # run/use and Google operates a free public one with no auth required,
    # so it's a sane default with zero setup. TURN (relaying actual media
    # through a third-party server when a direct peer-to-peer path can't
    # be established -- common across restrictive NATs/corporate
    # firewalls/some mobile carriers) is a fundamentally different,
    # non-free service requiring a real account and per-GB relay
    # bandwidth cost -- DISCLOSED GAP: no TURN server is configured here,
    # matching this codebase's own "inert until configured" posture for
    # every other paid third-party service (`pinata_jwt`, `r2_account_id`,
    # `smtp_host`). Calls between two peers on sufficiently open networks
    # (most home/office Wi-Fi, same-NAT, many mobile networks) will
    # connect fine over STUN alone; calls where both sides sit behind
    # symmetric NAT/strict firewalls will fail to establish a peer-to-peer
    # media path without a real TURN server -- a real deployment of this
    # feature needs one (e.g. Twilio Network Traversal Service, or a
    # self-hosted coturn) before this can be considered production-ready
    # for all users, not just favorably-networked ones.
    live_session_stun_urls: str = "stun:stun.l.google.com:19302"

    # How far ahead of a CONFIRMED `LiveSession.scheduled_at` the periodic
    # reminder scan (`app/workers/live_session_reminder_tasks.py`) should
    # notify both participants -- see that module's own docstring for the
    # full disclosed reasoning (why a config setting, not a hardcoded
    # constant like the Approaching Deadlines scan uses; why 60, not a
    # multi-day window).
    live_session_reminder_minutes_before: int = 60

    # APEX Staging Deployment Readiness: the app previously had NO CORS
    # configuration at all (confirmed: no `CORSMiddleware` anywhere in
    # this codebase before this step). That was invisible in every
    # deployment so far because the frontend has always called this
    # backend via a RELATIVE path (`const API_BASE = '/api/v1'`, same
    # convention in all 19 frontend pages that call the API) -- i.e.
    # same-origin, where CORS is irrelevant. It only becomes a real gap
    # the moment frontend and backend are deployed to two DIFFERENT
    # origins (e.g. a separate static host + a separate API host), which
    # is what a conventional staging split would do.
    #
    # Comma-separated list of exact allowed origins (e.g.
    # "https://staging-apex.example.com"), read from `CORS_ORIGINS`.
    # Deliberately NOT a wildcard, and deliberately opt-in: an empty
    # value (the default) adds the middleware with zero allowed origins,
    # which is the exact same "no cross-origin access" behavior this app
    # already had with no CORS middleware at all -- so this setting is
    # pure addition, not a behavior change, until a staging/production
    # deployment actually sets it.
    cors_origins: str = ""

    # ------------------------------------------------------------------
    # Embeddings (shared by Opportunity.embedding and the AI Opportunity
    # Agent). `embedding_dimension` MUST equal the pgvector column size
    # created by the migrations (1536 today) AND the vector length the
    # configured embedding model actually returns. This is checked at
    # startup (`app/agent/config_checks.py`) and again at runtime on every
    # embedding call -- a mismatch fails loudly instead of corrupting data.
    # Changing it requires a new Alembic migration that resizes the column
    # and re-embeds existing rows.
    # ------------------------------------------------------------------
    embedding_dimension: int = 1536

    # ------------------------------------------------------------------
    # APEX AI Opportunity Agent (app/agent/). Everything is opt-in and
    # provider-agnostic. No model name is assumed: if a provider is
    # selected, its model MUST be set explicitly via env vars, otherwise
    # startup fails with a clear message. Defaults keep the agent inert
    # (deterministic stages still work; LLM stages report a clear error).
    # ------------------------------------------------------------------
    # LLM used for extraction (and optionally writer/reviewer).
    agent_llm_provider: str = "none"  # gemini | openrouter | ollama | none
    agent_llm_model: str | None = None
    # Optional second provider tried when the primary fails with a
    # transient/rate-limit/auth error (never on a validation failure).
    agent_llm_fallback_provider: str | None = None
    agent_llm_fallback_model: str | None = None
    agent_llm_timeout_seconds: float = 60.0
    agent_llm_temperature: float = 0.1
    # Bounded self-correction: extra attempts AFTER the first one when the
    # model output fails Pydantic validation (the errors are sent back).
    agent_llm_max_validation_retries: int = 2
    # Ordered, FREE-ONLY routing chain for the Agent (takes precedence over
    # AGENT_LLM_PROVIDER/MODEL + FALLBACK_* when set). Comma-separated
    # "provider:model" entries, provider in gemini | groq | cloudflare |
    # openrouter | ollama.
    # Only the first ":" separates provider from model, so OpenRouter ids
    # such as "vendor/model:free" work. Example:
    #   gemini:gemini-3.8-flash,gemini:gemini-3.5-flash-lite,
    #   openrouter:<vendor/model>:free,ollama:<local-model>
    agent_llm_chain: str = ""
    # Bounded in-call retries per provider on 429/503/timeouts (0-3), and
    # the longest retry-after the router will sleep for before moving on.
    agent_llm_retries_per_provider: int = 1
    agent_llm_max_retry_wait_seconds: float = 20.0
    # Seconds a provider is skipped after exhausting retries (rate limit /
    # outage) and after an auth / model-unavailable / cost-policy error.
    agent_llm_cooldown_seconds: float = 60.0
    agent_llm_disable_seconds: float = 3600.0
    # Gemini models the Agent may use. Mirrors the models Google lists with
    # a FREE tier on https://ai.google.dev/gemini-api/docs/pricing (checked
    # Oct 2026; Pro models are paid-only). Any other Gemini model in the
    # chain is refused at startup. Re-check the pricing page when changing.
    agent_gemini_free_models: str = (
        "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,"
        "gemini-3.5-flash-lite,gemini-3.1-flash-lite"
    )
    # Groq (reuses GROQ_API_KEY above). The API cannot tell a Free-tier org
    # from a paid Developer-tier one, so the agent refuses to use Groq until
    # the operator confirms the org is on the Free tier.
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_free_tier_confirmed: bool = False
    # Cloudflare Workers AI. Usage past the daily free allocation is BILLED
    # on Workers Paid (it errors on Workers Free) and the token cannot read
    # the plan, so the agent refuses to use it until the operator confirms
    # the account is on Workers Free.
    cloudflare_api_token: str | None = None
    cloudflare_account_id: str | None = None
    cloudflare_workers_free_plan_confirmed: bool = False
    # Local Ollama (or LM Studio / llama.cpp) OpenAI-compatible endpoint.
    # Must be a local/private address; public URLs are refused.
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_api_key: str | None = None

    # OpenRouter-compatible endpoint (any OpenAI-compatible
    # /chat/completions server works, e.g. a local Ollama/LM Studio).
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_app_title: str = "APEX Opportunity Agent"
    openrouter_http_referer: str | None = None

    # Embeddings for semantic duplicate CANDIDATES and historical RAG.
    # "none" disables the semantic layer (dedup stays deterministic, RAG
    # falls back to lexical similarity).
    agent_embedding_provider: str = "none"  # openai | gemini | none
    agent_embedding_model: str | None = None

    # Writer: "template" = deterministic APEX format, zero AI cost.
    # "llm" = LLM writer with deterministic fact guard + template fallback.
    agent_writer_mode: str = "template"
    # Reviewer: deterministic quality gate always runs; the LLM review pass
    # is an optional extra (one call) on top of it.
    agent_reviewer_llm_enabled: bool = False
    agent_max_revision_rounds: int = 2

    # Persistent job runner.
    agent_job_max_attempts: int = 5
    agent_job_backoff_base_seconds: int = 30
    agent_job_backoff_max_seconds: int = 3600
    agent_job_lease_seconds: int = 300
    agent_job_batch_size: int = 10

    # Deduplication.
    agent_semantic_candidate_threshold: float = 0.85
    agent_semantic_candidate_limit: int = 5

    # Official-source verification: fetch the application/official page
    # (plain HTTP GET, size/time capped, robots-respecting user agent).
    agent_official_page_fetch_enabled: bool = True
    agent_official_page_timeout_seconds: float = 10.0
    agent_official_page_max_bytes: int = 1_500_000

    # Comma-separated RSS/Atom feed URLs polled every 30 minutes (only
    # feeds the team chose; nothing else is crawled).
    agent_rss_feeds: str = ""
    # Kick the Celery pipeline task right after ingestion/approval (the
    # every-minute beat schedule is the safety net either way).
    agent_celery_trigger_enabled: bool = True

    # Publishing. Comma-separated publisher names that are ENABLED:
    # mock | manual | telegram. Real publishers also need their own config.
    agent_publishers: str = "manual"
    telegram_bot_token: str | None = None
    telegram_publish_chat_id: str | None = None

    # WhatsApp ingestion. `whatsapp_connector`: disabled | mock | cloud_api | bridge.
    whatsapp_connector: str = "disabled"
    # Comma-separated allowlists. A group message is accepted ONLY if its
    # group id is listed; a direct (1:1) message ONLY if its sender is listed.
    allowed_whatsapp_group_ids: str = ""
    allowed_whatsapp_sender_ids: str = ""
    # The APEX bot's own WhatsApp id(s) / phone number(s): messages from
    # these are dropped (bot-loop protection), in addition to `from_me`.
    whatsapp_bot_ids: str = ""
    # Generic bridge (any self-hosted connector that POSTs normalized
    # events): HMAC-SHA256 secret for the X-Apex-Signature header.
    whatsapp_bridge_hmac_secret: str | None = None
    # Official Meta WhatsApp Cloud API webhook.
    whatsapp_cloud_app_secret: str | None = None
    whatsapp_cloud_verify_token: str | None = None

    @property
    def cors_allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @staticmethod
    def _csv(value: str | None) -> list[str]:
        return [item.strip() for item in (value or "").split(",") if item.strip()]

    @property
    def allowed_whatsapp_group_id_set(self) -> frozenset[str]:
        return frozenset(self._csv(self.allowed_whatsapp_group_ids))

    @property
    def allowed_whatsapp_sender_id_set(self) -> frozenset[str]:
        return frozenset(self._csv(self.allowed_whatsapp_sender_ids))

    @property
    def whatsapp_bot_id_set(self) -> frozenset[str]:
        return frozenset(self._csv(self.whatsapp_bot_ids))

    @property
    def agent_llm_chain_entries(self) -> list[tuple[str, str | None]]:
        """(provider, model) pairs of the Agent routing chain. Falls back to
        the legacy AGENT_LLM_PROVIDER/MODEL + AGENT_LLM_FALLBACK_* pair."""
        entries: list[tuple[str, str | None]] = []
        if self.agent_llm_chain.strip():
            for item in self._csv(self.agent_llm_chain):
                provider, _, model = item.partition(":")
                entries.append((provider.strip().lower(), model.strip() or None))
            return entries
        legacy: list[tuple[str | None, str | None]] = [
            (self.agent_llm_provider, self.agent_llm_model),
            (self.agent_llm_fallback_provider, self.agent_llm_fallback_model),
        ]
        for provider, model in legacy:
            if provider and provider.lower() != "none":
                entries.append((provider.lower(), model))
        return entries

    @property
    def agent_gemini_free_model_set(self) -> frozenset[str]:
        return frozenset(self._csv(self.agent_gemini_free_models))

    @property
    def agent_publisher_names(self) -> list[str]:
        return [name.lower() for name in self._csv(self.agent_publishers)]


def get_settings() -> Settings:
    """Cached settings accessor so we don't re-parse env vars on every call."""
    return Settings()


settings = get_settings()
