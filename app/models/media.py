import enum
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.db.session import Base


class MediaType(str, enum.Enum):
    IMAGE = "image"
    VIDEO = "video"
    GIF = "gif"


class ProcessingStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Media(Base):
    __tablename__ = "media"

    id = Column(Integer, primary_key=True, index=True)

    # Owner information
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    owner = relationship("User", back_populates="media_items")

    # File information
    original_filename = Column(String, nullable=False)
    processed_filename = Column(String, nullable=True)
    original_path = Column(String, nullable=False)
    processed_path = Column(String, nullable=True)
    media_type = Column(Enum(MediaType), nullable=False)
    file_size = Column(Integer, nullable=False)  # Size in bytes

    # Processing information
    status = Column(Enum(ProcessingStatus), default=ProcessingStatus.PENDING)
    progress = Column(Float, default=0.0)  # Progress as percentage (0-100)
    error_message = Column(String, nullable=True)
    processing_started_at = Column(DateTime, nullable=True)
    processing_completed_at = Column(DateTime, nullable=True)

    # Content information
    title = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    tags = Column(ARRAY(String), nullable=True)
    metadata = Column(
        JSONB,
        nullable=True,
    )  # Store additional metadata like dimensions, duration, etc.
    ocr_text = Column(Text, nullable=True)  # Text extracted from the media

    # System fields
    is_public = Column(Boolean, default=True)
    is_deleted = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Media {self.id}: {self.original_filename}>"
