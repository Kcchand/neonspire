import os
from celery import Celery
from kombu import Exchange, Queue


# -------------------------
# Celery factory
# -------------------------
def make_celery():
    broker_url = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
    result_backend = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")

    celery = Celery(
        "crypto_casino",
        broker=broker_url,
        backend=result_backend,
        include=["id_requests"],  # make sure tasks in id_requests.py are loaded
    )

    # ---- Queue setup: dedicated queue for ID requests ----
    celery.conf.task_queues = (
        Queue("id_requests", Exchange("id_requests"), routing_key="id_requests"),
    )
    celery.conf.task_default_queue = "id_requests"
    celery.conf.task_default_exchange = "id_requests"
    celery.conf.task_default_routing_key = "id_requests"

    # ---- NeonSpire production tuning (stability) ----
    celery.conf.update(
        # Fair scheduling: don't let one worker hoard a bunch of tasks
        worker_prefetch_multiplier=1,

        # If a worker dies mid-task, Celery will re-queue the task
        task_acks_late=True,

        # Redis visibility timeout (seconds) – tasks are not lost if worker dies
        broker_transport_options={
            "visibility_timeout": 3600,   # 1 hour
        },

        # Safety limits for very long provider runs
        task_soft_time_limit=300,        # 5 minutes soft limit
        task_time_limit=360,             # 6 minutes hard kill
    )

    return celery


celery = make_celery()


def get_flask_app():
    """
    Helper for Celery tasks to get the Flask app with all extensions loaded.
    Imported lazily to avoid circular imports at module import time.
    """
    # app.py at project root defines the Flask app
    from app import app
    return app
