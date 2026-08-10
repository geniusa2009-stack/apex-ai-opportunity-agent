import os
os.environ.setdefault("SUPABASE_JWT_SECRET", "x")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("SYNC_DATABASE_URL", "postgresql://u:p@localhost/db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("CELERY_BROKER_URL", "redis://localhost:6379/1")
os.environ.setdefault("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")
os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "x")
from app.main import app
paths = [p for p in app.openapi()["paths"].keys() if "notification" in p]
print("NOTIFICATION PATHS:", paths)
schema = app.openapi()
print("TOTAL PATHS:", len(schema["paths"]))
