from celery import Celery

from app.config import settings

celery_app = Celery(
    "media_processor",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.celery_worker.tasks"],
)

# Configure Celery
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    worker_max_tasks_per_child=1000,
    task_time_limit=3600,  # 1 hour timeout
    task_soft_time_limit=3000,  # Soft timeout of 50 minutes
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_send_task_events=True,
    task_send_sent_event=True,
    broker_connection_retry=True,
    broker_connection_retry_on_startup=True,
)

# Celery beat scheduler settings
celery_app.conf.beat_schedule = {
    "cleanup_expired_media": {
        "task": "app.celery_worker.tasks.cleanup_expired_media",
        "schedule": 86400.0,  # Run daily
    },
}

if __name__ == "__main__":
    celery_app.start()
