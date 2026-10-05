"""Live HTTP proof of the AI Opportunity Agent (see TESTING.md).

Real uvicorn process + a real, migrated, DISPOSABLE Postgres (+pgvector) +
real Supabase-shaped JWT auth over real HTTP. Only the LLM, the official
page and the publisher are fakes, so no API key or network is needed:

    WhatsApp (mock connector) webhook -> pipeline -> approval queue over HTTP
    -> approve over HTTP -> publish (mock) -> performance + metrics over HTTP

Required env: DATABASE_URL (asyncpg URL of the disposable DB -- it is
TRUNCATED), SUPABASE_JWT_SECRET and the other required settings,
WHATSAPP_CONNECTOR=mock, ALLOWED_WHATSAPP_GROUP_IDS=120363@g.us,
AGENT_PUBLISHERS=mock, AGENT_CELERY_TRIGGER_ENABLED=false.
Run from the repository root: python scripts/agent_live_proof.py
"""
import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
import time
import uuid
import urllib.request

import jwt

BASE = "http://127.0.0.1:8765"
ENV = dict(os.environ)
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f" -- {extra}" if extra and not cond else ""))


def http(method, path, body=None, token=None, raw=None, headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode()
            return r.status, (json.loads(text) if text and text[0] in "[{" else text)
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        return e.code, (json.loads(text) if text and text[0] in "[{" else text)


def token_for(user_id, email):
    now = int(time.time())
    return jwt.encode({"sub": str(user_id), "email": email, "aud": "authenticated", "iat": now, "exp": now + 3600},
                      ENV["SUPABASE_JWT_SECRET"], algorithm="HS256")


async def db_setup_and_pipeline(stage):
    sys.path.insert(0, ".")
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from app.agent.llm import LLMClient
    from app.agent.llm.fake import FakeLLMProvider
    from app.agent.pipeline import AgentDependencies, run_due_jobs
    from app.agent.publishing import MockPublisher
    from app.models.user import User
    from app.models.enums import AdminRole
    from tests.agent_support import sample_extraction
    from tests.test_agent_pipeline_e2e import FakeFetcher

    engine = create_async_engine(ENV["DATABASE_URL"], poolclass=NullPool)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        if stage == "reset":
            async with engine.begin() as conn:
                await conn.execute(text("TRUNCATE post_performance, publications, pipeline_job_events, agent_post_drafts, pipeline_jobs, opportunity_source_messages, opportunity_fingerprints, agent_opportunity_facts, source_messages, evidence, claims, opportunity_change_history, moderation_log_entries, opportunities, sources CASCADE"))
                await conn.execute(text("DELETE FROM users WHERE email LIKE 'live-%'"))
            async with factory() as db:
                mod = User(id=uuid.uuid4(), email="live-mod@apex.test", full_name="Live Mod", is_active=True, is_superuser=False, admin_role=AdminRole.MODERATOR)
                student = User(id=uuid.uuid4(), email="live-student@apex.test", full_name="Student", is_active=True, is_superuser=False)
                db.add_all([mod, student]); await db.commit()
                return mod.id, student.id
        deps = AgentDependencies(llm=LLMClient(primary=FakeLLMProvider([sample_extraction()])), fetcher=FakeFetcher(),
                                 publishers=[MockPublisher()], today=lambda: dt.date(2026, 10, 5))
        return await run_due_jobs(factory, deps)
    finally:
        await engine.dispose()


def main():
    mod_id, student_id = asyncio.run(db_setup_and_pipeline("reset"))
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8765", "--log-level", "warning"],
                              env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                if http("GET", "/health")[0] == 200: break
            except Exception: time.sleep(0.5)
        mod = token_for(mod_id, "live-mod@apex.test"); student = token_for(student_id, "live-student@apex.test")
        check("health 200", http("GET", "/health") == (200, {"status": "ok"}))
        code, body = http("GET", "/ready")
        check("ready reports db ok + embedding ok + redis unavailable (503)", code == 503 and body["checks"]["database"] == "ok" and body["checks"]["embedding_dimension"] == "ok" and body["checks"]["redis"].startswith("unavailable"), body)
        code, spec = http("GET", "/openapi.json")
        check("openapi builds and lists agent routes", code == 200 and "/api/v1/admin/agent/approvals" in spec["paths"] and "/api/v1/agent/whatsapp/webhook" in spec["paths"])
        check("no token -> 401/403", http("GET", "/api/v1/admin/agent/approvals")[0] in (401, 403))
        check("student -> 403", http("GET", "/api/v1/admin/agent/approvals", token=student)[0] == 403)

        from tests.agent_support import SAMPLE_OPPORTUNITY_TEXT

        msg = {"message_id": "live-1", "sender_id": "201000000001", "group_id": "120363@g.us", "group_name": "APEX",
               "text": SAMPLE_OPPORTUNITY_TEXT}
        code, body = http("POST", "/api/v1/agent/whatsapp/webhook", {"messages": [msg, dict(msg, message_id="x2", group_id="intruder@g.us"), dict(msg, message_id="x3", from_me=True)]})
        check("webhook: 1 accepted, intruder + bot refused", code == 200 and body["accepted"] == 1 and body["by_status"].get("unauthorized_group") == 1 and body["by_status"].get("ignored_self") == 1, body)
        code, body = http("POST", "/api/v1/agent/whatsapp/webhook", {"messages": [msg]})
        check("webhook redelivery is idempotent", body["by_status"] == {"duplicate_delivery": 1}, body)

        summary = asyncio.run(db_setup_and_pipeline("run"))
        check("pipeline reached waiting_approval", summary == {"waiting_approval": 1}, summary)
        code, queue = http("GET", "/api/v1/admin/agent/approvals", token=mod)
        check("approval queue has the draft", code == 200 and len(queue) == 1 and queue[0]["verification"]["level"] == "official_confirmed", queue)
        draft_id = queue[0]["draft"]["id"]
        code, opp_list = http("GET", "/api/v1/opportunities/", token=student)
        check("draft opportunity NOT visible to students before approval", code == 200 and opp_list.get("total", len(opp_list.get("items", []))) == 0, opp_list)
        code, body = http("POST", f"/api/v1/admin/agent/drafts/{draft_id}/approve", {"notes": "live proof"}, token=mod)
        check("approve -> publishing", code == 200 and body["job_status"] == "publishing", body)
        check("double approve -> 409", http("POST", f"/api/v1/admin/agent/drafts/{draft_id}/approve", {}, token=mod)[0] == 409)
        summary = asyncio.run(db_setup_and_pipeline("run"))
        check("publish job completed", summary == {"completed": 1}, summary)
        code, pubs = http("GET", "/api/v1/admin/agent/publications", token=mod)
        check("one publication, published", code == 200 and len(pubs) == 1 and pubs[0]["status"] == "published", pubs)
        code, opp_list = http("GET", "/api/v1/opportunities/", token=student)
        check("approved opportunity visible to students", code == 200 and opp_list.get("total", 0) == 1, opp_list)
        code, perf = http("POST", f"/api/v1/admin/agent/publications/{pubs[0]['id']}/performance", {"views": 200, "reactions": 30}, token=mod)
        check("performance recorded", code == 201 and perf["engagement_score"] == 0.15, perf)
        code, metrics = http("GET", "/api/v1/admin/agent/metrics", token=mod)
        check("metrics", code == 200 and metrics["opportunities_received"] == 1 and metrics["publications_successful"] == 1, metrics)
        code, sub = http("POST", "/api/v1/agent/submissions", {"text": "Volunteer found: Google STEP internship 2027, apply https://buildyourfuture.withgoogle.com/programs/step"}, token=mod)
        check("volunteer submission queued", code == 202 and sub["status"] == "queued", sub)
        code, jobs_ = http("GET", "/api/v1/admin/agent/jobs", token=mod)
        check("jobs listing", code == 200 and len(jobs_) == 2)
        code, detail = http("GET", f"/api/v1/admin/agent/jobs/{queue[0]['draft']['job_id']}", token=mod)
        check("job audit trail over HTTP", code == 200 and "human_approved" in [e["stage"] for e in detail["events"]])
    finally:
        server.terminate(); server.wait(10)
    passed = sum(ok for _, ok in results)
    print(f"\nLIVE PROOF: {passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
