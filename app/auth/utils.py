from typing import Optional

from sqlalchemy.orm import Session

from app.auth.jwt import get_password_hash, verify_password
from app.models.user import User
from app.schemas.user import UserCreate


def authenticate_user(db: Session, username_or_email: str, password: str) -> Optional[User]:
    """
    Authenticate a user with username/email and password.

    Args:
        db: Database session
        username_or_email: Username or email of the user
        password: Plain password to verify

    Returns:
        User if authentication is successful, None otherwise
    """
    user = get_user_by_username_or_email(db, username_or_email)
    if not user:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


def get_user_by_username_or_email(db: Session, username_or_email: str) -> Optional[User]:
    """
    Get a user by username or email.

    Args:
        db: Database session
        username_or_email: Username or email of the user

    Returns:
        User if found, None otherwise
    """
    return (
        db.query(User)
        .filter(
            (User.email == username_or_email) | (User.username == username_or_email),
        )
        .first()
    )


def create_user(db: Session, user_in: UserCreate) -> User:
    """
    Create a new user.

    Args:
        db: Database session
        user_in: User creation data

    Returns:
        Created user
    """
    db_user = User(
        email=user_in.email,
        username=user_in.username,
        hashed_password=get_password_hash(user_in.password),
        full_name=user_in.full_name,
        is_admin=user_in.is_admin if hasattr(user_in, "is_admin") else False,
        is_superuser=user_in.is_superuser if hasattr(user_in, "is_superuser") else False,
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user
