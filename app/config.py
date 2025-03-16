import secrets
from typing import Any, Dict, List, Optional
from wsgiref.validate import validator

from pydantic import AnyHttpUrl, DirectoryPath, EmailStr, PostgresDsn
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # API settings
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str = secrets.token_urlsafe(32)
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # App settings
    APP_NAME: str = "Media Search Platform"
    DEBUG: bool = False

    # CORS settings
    BACKEND_CORS_ORIGINS: List[AnyHttpUrl] = []

    # Database settings
    POSTGRES_SERVER: str
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_DB: str
    POSTGRES_PORT: str
    SQLALCHEMY_DATABASE_URI: Optional[PostgresDsn] = None

    @classmethod
    @validator("SQLALCHEMY_DATABASE_URI", pre=True)
    def assemble_db_connection(cls, v: Optional[str], values: Dict[str, Any]) -> Any:
        if isinstance(v, str):
            return v
        return PostgresDsn.build(
            scheme="postgresql",
            user=values.get("POSTGRES_USER"),
            password=values.get("POSTGRES_PASSWORD"),
            host=values.get("POSTGRES_SERVER"),
            port=values.get("POSTGRES_PORT", "5432"),
            path=f"/{values.get('POSTGRES_DB') or ''}",
        )

    # Redis settings
    REDIS_HOST: str
    REDIS_PORT: int
    CELERY_BROKER_URL: Optional[str] = None
    CELERY_RESULT_BACKEND: Optional[str] = None

    @classmethod
    @validator("CELERY_BROKER_URL", "CELERY_RESULT_BACKEND", pre=True)
    def assemble_redis_connection(cls, v: Optional[str], values: Dict[str, Any]) -> Any:
        if isinstance(v, str):
            return v
        return f"redis://{values.get('REDIS_HOST')}:{values.get('REDIS_PORT')}/0"

    # Media settings
    MEDIA_DIR: DirectoryPath
    UPLOAD_DIR: DirectoryPath
    PROCESSED_DIR: DirectoryPath
    MAX_VIDEO_LENGTH_SECONDS: int = 10
    ALLOWED_IMAGE_EXTENSIONS: List[str] = ["jpg", "jpeg", "png", "gif", "webp"]
    ALLOWED_VIDEO_EXTENSIONS: List[str] = ["mp4", "avi", "mov", "webm", "mkv"]
    MAX_UPLOAD_SIZE_MB: int = 50

    # Google Gemini API
    GOOGLE_SERVICE_ACCOUNT_KEY: str
    GEMINI_API_ENDPOINT: str

    # Admin settings
    ADMIN_EMAIL: EmailStr
    ADMIN_PASSWORD: str
    ADMIN_USERNAME: str

    # Default prompt
    DEFAULT_MEDIA_PROMPT: str = """
    Please provide a comprehensive analysis of this media file for search indexing purposes:

    1. VISUAL DESCRIPTION:
       - Describe all visible objects, people, animals, or scenes
       - Note any actions, movements, or events occurring
       - Mention colors, setting, and overall atmosphere

    2. TEXT EXTRACTION:
       - Transcribe any visible text exactly as it appears

    3. CONTENT CONTEXT:
       - What is the apparent purpose of this media?
       - What mood or emotion does it convey?
       - Any cultural or historical references visible?

    4. SEARCH TAGS:
       - Format as JSON array ["tag1", "tag2", "tag3", ...]
       - Include at least 10-15 specific keywords
       - Include both general categories and specific descriptors

    Please format your response with clear section headers and ensure the SEARCH TAGS are in a properly formatted JSON array.
    """

    class Config:
        case_sensitive = True
        env_file = ".env"


settings = Settings()
