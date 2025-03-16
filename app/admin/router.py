from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.admin.dependencies import get_admin_user
from app.auth.jwt import get_password_hash
from app.auth.utils import create_user
from app.db.session import get_db
from app.models.media import Media as MediaModel
from app.models.prompt import Prompt as PromptModel
from app.models.user import User as UserModel
from app.schemas.user import AdminUserCreate, User

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/dashboard", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    current_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Admin dashboard page.
    """
    # Get stats
    total_users = db.query(UserModel).count()
    total_media = db.query(MediaModel).count()
    active_prompts = db.query(PromptModel).filter(PromptModel.is_active is True).count()

    # Get recent users
    recent_users = db.query(UserModel).order_by(UserModel.created_at.desc()).limit(5).all()

    # Get recent media
    recent_media = db.query(MediaModel).order_by(MediaModel.created_at.desc()).limit(5).all()

    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "user": current_user,
            "total_users": total_users,
            "total_media": total_media,
            "active_prompts": active_prompts,
            "recent_users": recent_users,
            "recent_media": recent_media,
        },
    )


# User management


@router.get("/users", response_class=HTMLResponse)
async def list_users(
    request: Request,
    current_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    List all users.
    """
    users = db.query(UserModel).order_by(UserModel.created_at.desc()).all()

    return templates.TemplateResponse(
        "admin/users.html",
        {
            "request": request,
            "user": current_user,
            "users": users,
        },
    )


@router.get("/users/create", response_class=HTMLResponse)
async def create_user_form(
    request: Request,
    current_user: User = Depends(get_admin_user),
):
    """
    User creation form.
    """
    return templates.TemplateResponse(
        "admin/user_edit.html",
        {
            "request": request,
            "user": current_user,
            "is_new": True,
        },
    )


@router.post("/users/create")
async def create_user_submit(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(None),
    is_active: bool = Form(True),
    is_admin: bool = Form(False),
    current_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Create a new user.
    """
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
            "admin/user_edit.html",
            {
                "request": request,
                "user": current_user,
                "is_new": True,
                "error": "User with this email or username already exists",
                "username": username,
                "email": email,
                "full_name": full_name,
                "is_active": is_active,
                "is_admin": is_admin,
            },
        )

    # Create user
    user_in = AdminUserCreate(
        username=username,
        email=email,
        password=password,
        full_name=full_name,
        is_active=is_active,
        is_admin=is_admin,
    )
    user = create_user(db, user_in)

    return RedirectResponse(
        url="/admin/users",
        status_code=status.HTTP_302_FOUND,
    )


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
async def edit_user_form(
    request: Request,
    user_id: int,
    current_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    User edit form.
    """
    edit_user = db.query(UserModel).filter(UserModel.id == user_id).first()

    if not edit_user:
        return templates.TemplateResponse(
            "admin/user_edit.html",
            {
                "request": request,
                "user": current_user,
                "error": "User not found",
            },
            status_code=404,
        )

    return templates.TemplateResponse(
        "admin/user_edit.html",
        {
            "request": request,
            "user": current_user,
            "edit_user": edit_user,
            "is_new": False,
        },
    )


@router.post("/users/{user_id}/edit")
async def edit_user_submit(
    request: Request,
    user_id: int,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(None),
    full_name: str = Form(None),
    is_active: bool = Form(True),
    is_admin: bool = Form(False),
    current_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Update a user.
    """
    edit_user = db.query(UserModel).filter(UserModel.id == user_id).first()

    if not edit_user:
        return templates.TemplateResponse(
            "admin/user_edit.html",
            {
                "request": request,
                "user": current_user,
                "error": "User not found",
            },
            status_code=404,
        )

    # Check for duplicate username/email
    duplicate = (
        db.query(UserModel)
        .filter(
            (UserModel.id != user_id)
            & ((UserModel.email == email) | (UserModel.username == username)),
        )
        .first()
    )

    if duplicate:
        return templates.TemplateResponse(
            "admin/user_edit.html",
            {
                "request": request,
                "user": current_user,
                "edit_user": edit_user,
                "is_new": False,
                "error": "Another user with this email or username already exists",
            },
        )

    # Update user
    edit_user.username = username
    edit_user.email = email
    edit_user.full_name = full_name
    edit_user.is_active = is_active
    edit_user.is_admin = is_admin

    if password:
        edit_user.hashed_password = get_password_hash(password)

    db.commit()

    return RedirectResponse(
        url="/admin/users",
        status_code=status.HTTP_302_FOUND,
    )


@router.post("/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    current_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Delete a user.
    """
    # Prevent self-deletion
    if current_user.id == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )

    user = db.query(UserModel).filter(UserModel.id == user_id).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    db.delete(user)
    db.commit()

    return {"status": "success"}


# Prompt management


@router.get("/prompts", response_class=HTMLResponse)
async def list_prompts(
    request: Request,
    current_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    List all prompts.
    """
    prompts = db.query(PromptModel).order_by(PromptModel.created_at.desc()).all()

    return templates.TemplateResponse(
        "admin/prompts.html",
        {
            "request": request,
            "user": current_user,
            "prompts": prompts,
        },
    )


@router.get("/prompts/create", response_class=HTMLResponse)
async def create_prompt_form(
    request: Request,
    current_user: User = Depends(get_admin_user),
):
    """
    Prompt creation form.
    """
    return templates.TemplateResponse(
        "admin/prompt_edit.html",
        {
            "request": request,
            "user": current_user,
            "is_new": True,
        },
    )


@router.post("/prompts/create")
async def create_prompt_submit(
    request: Request,
    name: str = Form(...),
    description: str = Form(None),
    prompt_text: str = Form(...),
    is_default: bool = Form(False),
    is_active: bool = Form(True),
    current_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Create a new prompt.
    """
    # Check if prompt with same name exists
    existing = db.query(PromptModel).filter(PromptModel.name == name).first()

    if existing:
        return templates.TemplateResponse(
            "admin/prompt_edit.html",
            {
                "request": request,
                "user": current_user,
                "is_new": True,
                "error": "Prompt with this name already exists",
                "name": name,
                "description": description,
                "prompt_text": prompt_text,
                "is_default": is_default,
                "is_active": is_active,
            },
        )

    # Create prompt
    prompt = PromptModel(
        name=name,
        description=description,
        prompt_text=prompt_text,
        is_default=is_default,
        is_active=is_active,
    )

    # If this is set as default, unset any other defaults
    if is_default:
        db.query(PromptModel).filter(PromptModel.is_default is True).update(
            {"is_default": False},
        )

    db.add(prompt)
    db.commit()

    return RedirectResponse(
        url="/admin/prompts",
        status_code=status.HTTP_302_FOUND,
    )


@router.get("/prompts/{prompt_id}/edit", response_class=HTMLResponse)
async def edit_prompt_form(
    request: Request,
    prompt_id: int,
    current_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Prompt edit form.
    """
    prompt = db.query(PromptModel).filter(PromptModel.id == prompt_id).first()

    if not prompt:
        return templates.TemplateResponse(
            "admin/prompt_edit.html",
            {
                "request": request,
                "user": current_user,
                "error": "Prompt not found",
            },
            status_code=404,
        )

    return templates.TemplateResponse(
        "admin/prompt_edit.html",
        {
            "request": request,
            "user": current_user,
            "prompt": prompt,
            "is_new": False,
        },
    )


@router.post("/prompts/{prompt_id}/edit")
async def edit_prompt_submit(
    request: Request,
    prompt_id: int,
    name: str = Form(...),
    description: str = Form(None),
    prompt_text: str = Form(...),
    is_default: bool = Form(False),
    is_active: bool = Form(True),
    current_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Update a prompt.
    """
    prompt = db.query(PromptModel).filter(PromptModel.id == prompt_id).first()

    if not prompt:
        return templates.TemplateResponse(
            "admin/prompt_edit.html",
            {
                "request": request,
                "user": current_user,
                "error": "Prompt not found",
            },
            status_code=404,
        )

    # Check for duplicate name
    duplicate = (
        db.query(PromptModel)
        .filter(
            (PromptModel.id != prompt_id) & (PromptModel.name == name),
        )
        .first()
    )

    if duplicate:
        return templates.TemplateResponse(
            "admin/prompt_edit.html",
            {
                "request": request,
                "user": current_user,
                "prompt": prompt,
                "is_new": False,
                "error": "Another prompt with this name already exists",
            },
        )

    # Update prompt
    prompt.name = name
    prompt.description = description
    prompt.prompt_text = prompt_text
    prompt.is_active = is_active

    # Handle default flag
    if is_default and not prompt.is_default:
        # Unset any other defaults
        db.query(PromptModel).filter(PromptModel.is_default is True).update(
            {"is_default": False},
        )
        prompt.is_default = True
    if not is_default and prompt.is_default:
        # If this was the default and we're unsetting it, find another to make default
        prompt.is_default = False
        if db.query(PromptModel).filter(PromptModel.is_default is True).count() == 0:
            # Make the first active prompt the default
            first_prompt = (
                db.query(PromptModel)
                .filter(
                    PromptModel.is_active is True,
                )
                .first()
            )
            if first_prompt:
                first_prompt.is_default = True

        db.commit()

        return RedirectResponse(
            url="/admin/prompts",
            status_code=status.HTTP_302_FOUND,
        )
    return None


@router.post("/prompts/{prompt_id}/delete")
async def delete_prompt(
    prompt_id: int,
    current_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Delete a prompt.
    """
    prompt = db.query(PromptModel).filter(PromptModel.id == prompt_id).first()

    if not prompt:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prompt not found",
        )

    # Don't allow deleting the default prompt
    if prompt.is_default:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete the default prompt",
        )

    db.delete(prompt)
    db.commit()

    return {"status": "success"}
