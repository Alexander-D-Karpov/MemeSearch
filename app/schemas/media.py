from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.models.media import MediaType, ProcessingStatus


# Shared properties
class MediaBase(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = []
    is_public: Optional[bool] = True


# Properties to receive via API on creation
class MediaCreate(MediaBase):
    pass


# Properties to receive via API on update
class MediaUpdate(MediaBase):
    pass


# Properties for a progress update
class MediaProgressUpdate(BaseModel):
    progress: float = Field(..., ge=0.0, le=100.0)
    status: Optional[ProcessingStatus] = None
    error_message: Optional[str] = None


# Properties to return via API
class Media(MediaBase):
    id: int
    owner_id: int
    original_filename: str
    processed_filename: Optional[str]
    media_type: MediaType
    status: ProcessingStatus
    progress: float
    file_size: int
    ocr_text: Optional[str]
    metadata: Optional[Dict[str, Any]]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Properties for file upload response
class MediaUploadResponse(BaseModel):
    id: int
    original_filename: str
    media_type: MediaType
    status: ProcessingStatus
    message: str


# Properties for media listing with pagination
class MediaListItem(BaseModel):
    id: int
    title: Optional[str]
    media_type: MediaType
    processed_filename: Optional[str]
    status: ProcessingStatus
    progress: float
    created_at: datetime
    tags: List[str]

    class Config:
        from_attributes = True


class MediaListResponse(BaseModel):
    items: List[MediaListItem]
    total: int
    page: int
    size: int
    pages: int


# Properties for detailed media view
class MediaDetail(Media):
    processing_started_at: Optional[datetime]
    processing_completed_at: Optional[datetime]
    original_path: str
    processed_path: Optional[str]
