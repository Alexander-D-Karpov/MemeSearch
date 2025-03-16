from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr


# Shared properties
class UserBase(BaseModel):
    email: Optional[EmailStr] = None
    username: Optional[str] = None
    is_active: Optional[bool] = True
    is_admin: Optional[bool] = False
    full_name: Optional[str] = None


# Properties to receive via API on creation
class UserCreate(UserBase):
    email: EmailStr
    username: str
    password: str


# Properties to receive via API on update
class UserUpdate(UserBase):
    password: Optional[str] = None


# Properties to return via API
class User(UserBase):
    id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Additional properties stored in DB
class UserInDB(User):
    hashed_password: str


# Properties for admin user creation/update
class AdminUserCreate(UserCreate):
    is_admin: bool = True
    is_superuser: Optional[bool] = False


class AdminUserUpdate(UserUpdate):
    is_admin: Optional[bool] = None
    is_superuser: Optional[bool] = None
