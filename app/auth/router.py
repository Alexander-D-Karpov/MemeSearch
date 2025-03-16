from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import jwt
from app.auth.utils import authenticate_user, create_user
from app.config import settings
from app.db.session import get_db
from app.models.user import User as UserModel
from app.schemas.token import RefreshToken, Token
from app.schemas.user import User, UserCreate

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.post("/login", response_model=Token)
def login_access_token(
    db: Session = Depends(get_db),
    form_data: OAuth2PasswordRequestForm = Depends(),
) -> Any:
    """
    OAuth2 compatible token login, get an access token for future requests.
    """
    user = authenticate_user(db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = jwt.create_access_token(
        subject=user.id,
        expires_delta=access_token_expires,
    )
    refresh_token = jwt.create_refresh_token(subject=user.id)

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
    }


@router.post("/refresh", response_model=Token)
def refresh_token(
    db: Session = Depends(get_db),
    refresh_token: RefreshToken = Body(...),
) -> Any:
    """
    Refresh access token using a refresh token.
    """
    try:
        payload = jwt.jwt.decode(
            refresh_token.refresh_token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
        )
        token_data = jwt.TokenPayload(**payload)

        # Check if it's a refresh token
        if payload.get("type") != "refresh":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token type",
                headers={"WWW-Authenticate": "Bearer"},
            )
    except (jwt.JWTError, jwt.ValidationError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    user = db.query(UserModel).filter(UserModel.id == token_data.sub).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user",
        )

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return {
        "access_token": jwt.create_access_token(
            subject=user.id,
            expires_delta=access_token_expires,
        ),
        "refresh_token": jwt.create_refresh_token(subject=user.id),
        "token_type": "bearer",
    }


@router.post("/register", response_model=User)
def register_user(
    *,
    db: Session = Depends(get_db),
    user_in: UserCreate,
) -> Any:
    """
    Register a new user.
    """
    # Check if the user already exists
    user = (
        db.query(UserModel)
        .filter(
            (UserModel.email == user_in.email) | (UserModel.username == user_in.username),
        )
        .first()
    )
    if user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A user with this email or username already exists",
        )

    # Create new user
    return create_user(db, user_in)


# Routes for Web UI
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@router.post("/login-form")
async def login_form(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = authenticate_user(db, username, password)
    if not user:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid username or password"},
        )

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = jwt.create_access_token(
        subject=user.id,
        expires_delta=access_token_expires,
    )

    response = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    response.set_cookie(key="access_token", value=f"Bearer {access_token}", httponly=True)

    return response


@router.post("/register-form")
async def register_form(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(None),
    db: Session = Depends(get_db),
):
    # Check if user exists
    user = (
        db.query(UserModel)
        .filter(
            (UserModel.email == email) | (UserModel.username == username),
        )
        .first()
    )
    if user:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "User with this email or username already exists"},
        )

    # Create user
    user_in = UserCreate(
        username=username,
        email=email,
        password=password,
        full_name=full_name,
    )
    user = create_user(db, user_in)

    # Create token
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = jwt.create_access_token(
        subject=user.id,
        expires_delta=access_token_expires,
    )

    response = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    response.set_cookie(key="access_token", value=f"Bearer {access_token}", httponly=True)

    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/auth/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie(key="access_token")
    return response
