# Import all models here for Alembic to detect them
from app.db.session import Base  # noqa
from app.models.media import Media  # noqa
from app.models.prompt import Prompt  # noqa
from app.models.user import User  # noqa
