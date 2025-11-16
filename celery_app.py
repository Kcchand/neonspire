# celery_app.py

import os
from celery import Celery

# Redis URLs (local dev)
BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")


def make_celery() -> Celery:
    """
    Create a bare Celery instance.

    We do NOT import the Flask app here to avoid circular imports
    when app.py imports player_bp → id_requests → celery_app.
    """
    celery = Celery(
        "crypto_casino",
        broker=BROKER_URL,
        backend=RESULT_BACKEND,
        include=["id_requests"],  # where our tasks live
    )

    celery.conf.update(
        timezone="UTC",
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        enable_utc=True,
    )

    return celery


# Global Celery instance (used by celery worker and tasks)
celery: Celery = make_celery()


def init_celery(app) -> Celery:
    """
    Called from app.py:

        from celery_app import init_celery, celery
        init_celery(app)

    This attaches Flask application context to all Celery tasks,
    so `db`, `current_app`, etc. work as expected.
    """
    # Optionally sync any app config keys into Celery
    celery.conf.update(app.config or {})

    # Wrap tasks so they run inside app.app_context()
    class ContextTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery.Task = ContextTask
    return celery