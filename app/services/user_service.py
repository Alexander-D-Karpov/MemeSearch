import logging
from typing import List, Optional

from sqlalchemy.orm import Session

from app.auth.jwt import get_password_hash, verify_password
from app.models.user import User
from app.schemas.user import UserCreate, UserUpdate

logger = logging.getLogger(__name__)


class UserService:
    """
    Service for user-related operations.
    """

    @staticmethod
    def get_by_id(db: Session, user_id: int) -> Optional[User]:
        """
        Get a user by ID.

        Args:
            db: Database session
            user_id: User ID

        Returns:
            User if found, None otherwise
        """
        return db.query(User).filter(User.id == user_id).first()

    @staticmethod
    def get_by_email(db: Session, email: str) -> Optional[User]:
        """
        Get a user by email.

        Args:
            db: Database session
            email: User email

        Returns:
            User if found, None otherwise
        """
        return db.query(User).filter(User.email == email).first()

    @staticmethod
    def get_by_username(db: Session, username: str) -> Optional[User]:
        """
        Get a user by username.

        Args:
            db: Database session
            username: Username

        Returns:
            User if found, None otherwise
        """
        return db.query(User).filter(User.username == username).first()

    @staticmethod
    def get_by_username_or_email(db: Session, username_or_email: str) -> Optional[User]:
        """
        Get a user by username or email.

        Args:
            db: Database session
            username_or_email: Username or email

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

    @classmethod
    def authenticate(cls, db: Session, username_or_email: str, password: str) -> Optional[User]:
        """
        Authenticate a user with username/email and password.

        Args:
            db: Database session
            username_or_email: Username or email
            password: Plain password

        Returns:
            User if authentication is successful, None otherwise
        """
        user = cls.get_by_username_or_email(db, username_or_email)
        if not user:
            return None
        if not verify_password(password, user.hashed_password):
            return None
        return user

    @staticmethod
    def create(db: Session, user_in: UserCreate) -> User:
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
            is_admin=getattr(user_in, "is_admin", False),
            is_superuser=getattr(user_in, "is_superuser", False),
        )
        db.add(db_user)
        db.commit()
        db.refresh(db_user)
        return db_user

    @staticmethod
    def update(db: Session, db_user: User, user_in: UserUpdate) -> User:
        """
        Update a user.

        Args:
            db: Database session
            db_user: User to update
            user_in: User update data

        Returns:
            Updated user
        """
        update_data = user_in.model_dump(exclude_unset=True)

        if "password" in update_data:
            update_data["hashed_password"] = get_password_hash(update_data.pop("password"))

        for field, value in update_data.items():
            setattr(db_user, field, value)

        db.add(db_user)
        db.commit()
        db.refresh(db_user)
        return db_user

    @staticmethod
    def list_users(
        db: Session,
        skip: int = 0,
        limit: int = 100,
        is_active: Optional[bool] = None,
        is_admin: Optional[bool] = None,
    ) -> List[User]:
        """
        Get a list of users.

        Args:
            db: Database session
            skip: Number of users to skip
            limit: Maximum number of users to return
            is_active: Filter by active status
            is_admin: Filter by admin status

        Returns:
            List of users
        """
        query = db.query(User)

        if is_active is not None:
            query = query.filter(User.is_active == is_active)

        if is_admin is not None:
            query = query.filter(User.is_admin == is_admin)

        return query.offset(skip).limit(limit).all()

    @staticmethod
    def delete(db: Session, user_id: int) -> bool:
        """
        Delete a user.

        Args:
            db: Database session
            user_id: User ID

        Returns:
            True if deleted, False otherwise
        """
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return False

        db.delete(user)
        db.commit()
        return True
