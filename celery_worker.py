# celery_worker.py
from app import create_app
from celery_app import init_celery, celery as celery_app


# Create the Flask app (same as gunicorn)
flask_app = create_app()

# Attach Flask app context to Celery
init_celery(flask_app)

# This is what `celery -A celery_worker.celery worker` will use
celery = celery_app
