"""Disclosed, deliberate second Celery-task package.

Every prior Celery task in this codebase lives under `app/workers/`
(`tasks.py`, `scholar_agent.py`, `memory_tasks.py`, `media_processing.py`).
The Student Video Profiles & AI-Moderated Upload System step's own ask
explicitly names this task's file path as `app/tasks/student_video.py` --
a NEW top-level package, not `app/workers/student_video.py`. Given how
specifically that path is called out as one of the ask's own numbered
component headings, it's honored literally here rather than silently
relocated into `app/workers/` for cross-codebase consistency -- but the
resulting inconsistency (two parallel Celery-task directories) is real
and worth knowing about; see the README's disclosed judgment calls for
this step.

Both `app.workers.*` and `app.tasks.*` are registered in
`app/core/celery_app.py`'s `include` list -- Celery doesn't care which
package a task's dotted name lives under, so this doesn't affect
anything functionally, only where a future contributor should expect to
find task code.
"""
