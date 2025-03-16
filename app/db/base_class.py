from typing import Any

from sqlalchemy.ext.declarative import as_declarative, declared_attr


@as_declarative()
class Base:
    """
    Base class for all database models.

    Provides automatic table name generation and common methods.
    """

    id: Any
    __name__: str

    # Generate __tablename__ automatically
    @classmethod
    @declared_attr
    def __tablename__(cls) -> str:
        """
        Generate table name based on class name.
        Converts CamelCase to snake_case.
        """
        # Convert camel case to snake case
        # e.g., "UserModel" -> "user_model"
        return "".join(["_" + c.lower() if c.isupper() else c for c in cls.__name__]).lstrip("_")
