import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from celery import Task, states
from sqlalchemy.orm import Session, sessionmaker

from app.celery_worker.celery_app import celery_app
from app.celery_worker.media_processor import MediaProcessor
from app.db.session import engine
from app.models.media import Media, MediaType, ProcessingStatus
from app.models.prompt import Prompt

# Create database session
session_factory = sessionmaker(bind=engine)

# Setup logger
logger = logging.getLogger(__name__)


class DatabaseTask(Task):
    """Task with database session handling."""

    _db = None

    @property
    def db(self) -> Session:
        if self._db is None:
            self._db = session_factory()
        return self._db

    def after_return(self, *args, **kwargs):
        if self._db is not None:
            self._db.close()
            self._db = None


@celery_app.task(bind=True, base=DatabaseTask)
def process_media(self, media_id: int) -> Dict[str, Any]:
    """
    Process a media file.

    Args:
        media_id: ID of the media to process

    Returns:
        Status info dictionary
    """
    # Get media from database
    media = self.db.query(Media).filter(Media.id == media_id).first()
    if not media:
        logger.error(f"Media with ID {media_id} not found")
        return {"status": "error", "message": f"Media with ID {media_id} not found"}

    try:
        # Update status to processing
        media.status = ProcessingStatus.PROCESSING
        media.processing_started_at = datetime.utcnow()
        media.progress = 0.0
        self.db.commit()

        # Create media processor
        processor = MediaProcessor()

        # Create directories if they don't exist
        os.makedirs(os.path.dirname(media.original_path), exist_ok=True)
        processed_dir = Path(os.environ.get("PROCESSED_DIR", "/app/media/processed"))
        os.makedirs(processed_dir, exist_ok=True)

        # Update progress
        media.progress = 10.0
        self.db.commit()
        self.update_state(state=states.STARTED, meta={"progress": 10.0})

        # Get file extension and determine output path
        base_filename = os.path.splitext(os.path.basename(media.original_path))[0]

        if media.media_type == MediaType.IMAGE:
            # Process image
            output_path = os.path.join(processed_dir, f"{base_filename}.png")
            metadata = processor.convert_image(media.original_path, output_path)
        if media.media_type in [MediaType.VIDEO, MediaType.GIF]:
            # Process video or GIF
            output_path = os.path.join(processed_dir, f"{base_filename}.mp4")
            metadata = processor.convert_video(media.original_path, output_path)
            raise ValueError(f"Unsupported media type: {media.media_type}")

        # Update progress
        media.progress = 40.0
        media.processed_path = output_path
        media.processed_filename = os.path.basename(output_path)
        media.metadata = metadata
        self.db.commit()
        self.update_state(state=states.STARTED, meta={"progress": 40.0})

        # Get default prompt
        prompt = self.db.query(Prompt).filter(Prompt.is_default is True).first()
        prompt_text = (
            prompt.prompt_text
            if prompt
            else os.environ.get(
                "DEFAULT_MEDIA_PROMPT",
                "Please analyze this media and provide a detailed description with relevant tags.",
            )
        )

        # Process with Gemini
        self.update_state(
            state=states.STARTED,
            meta={"progress": 50.0, "status": "Analyzing with AI"},
        )
        result = processor.process_with_gemini(output_path, prompt_text)

        # Update progress
        media.progress = 90.0
        self.db.commit()
        self.update_state(state=states.STARTED, meta={"progress": 90.0})

        # Update media with processing results
        media.description = result.get("description", "")
        media.ocr_text = result.get("ocr_text", "")
        media.tags = result.get("tags", [])

        # Mark as completed
        media.status = ProcessingStatus.COMPLETED
        media.progress = 100.0
        media.processing_completed_at = datetime.utcnow()
        self.db.commit()

        return {
            "status": "success",
            "media_id": media_id,
            "message": "Media processed successfully",
        }

    except Exception as e:
        logger.exception(f"Error processing media {media_id}: {str(e)}")

        # Update media with error
        if media:
            media.status = ProcessingStatus.FAILED
            media.error_message = str(e)
            media.progress = 0.0
            self.db.commit()

        return {"status": "error", "message": str(e)}


@celery_app.task(bind=True, base=DatabaseTask)
def cleanup_expired_media(self) -> Dict[str, Any]:
    """
    Cleanup expired media files.

    Returns:
        Status info dictionary
    """
    try:
        # Find media marked as deleted
        deleted_media = self.db.query(Media).filter(Media.is_deleted is True).all()

        count = 0
        for media in deleted_media:
            # Delete files if they exist
            if media.original_path and os.path.exists(media.original_path):
                os.remove(media.original_path)

            if media.processed_path and os.path.exists(media.processed_path):
                os.remove(media.processed_path)

            # Remove from database
            self.db.delete(media)
            count += 1

        self.db.commit()

        return {
            "status": "success",
            "message": f"Cleaned up {count} deleted media files",
            "count": count,
        }
    except Exception as e:
        logger.exception(f"Error cleaning up media: {str(e)}")
        return {"status": "error", "message": str(e)}
