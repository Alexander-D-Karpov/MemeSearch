from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi_pagination import add_pagination
from starlette.middleware.cors import CORSMiddleware

from app.admin.router import router as admin_router
from app.api.v1.router import api_router
from app.auth.dependencies import get_current_user
from app.auth.jwt import get_password_hash
from app.auth.router import router as auth_router
from app.config import settings
from app.db.base import Base
from app.db.session import engine, get_db
from app.models.prompt import Prompt as PromptModel
from app.models.user import User as UserModel
from app.schemas.user import User, UserCreate

# Create application
app = FastAPI(
    title=settings.APP_NAME,
    debug=settings.DEBUG,
)

# Set up CORS
if settings.BACKEND_CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(origin) for origin in settings.BACKEND_CORS_ORIGINS],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Mount static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Mount media files
app.mount("/media", StaticFiles(directory="media"), name="media")

# Add API router
app.include_router(api_router, prefix=settings.API_V1_STR)

# Add auth router
app.include_router(auth_router, prefix="/auth", tags=["auth"])

# Add admin router
app.include_router(admin_router, prefix="/admin", tags=["admin"])

# Add pagination
add_pagination(app)

# Templates
templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
def startup_event():
    """
    Initialize database and create default admin user and prompt.
    """
    # Create all tables
    Base.metadata.create_all(bind=engine)

    # Create a database session
    db = next(get_db())

    try:
        # Check if admin user exists, create if not
        admin = (
            db.query(UserModel)
            .filter(
                UserModel.email == settings.ADMIN_EMAIL,
            )
            .first()
        )

        if not admin:
            admin_user = UserCreate(
                email=settings.ADMIN_EMAIL,
                username=settings.ADMIN_USERNAME,
                password=settings.ADMIN_PASSWORD,
            )
            admin = UserModel(
                email=admin_user.email,
                username=admin_user.username,
                hashed_password=get_password_hash(admin_user.password),
                is_admin=True,
                is_superuser=True,
                is_active=True,
            )
            db.add(admin)
            db.commit()
            db.refresh(admin)

        # Check if default prompt exists, create if not
        default_prompt = (
            db.query(PromptModel)
            .filter(
                PromptModel.is_default is True,
            )
            .first()
        )

        if not default_prompt:
            default_prompt = PromptModel(
                name="Default Media Analysis Prompt",
                description="Default prompt for analyzing media files",
                prompt_text=settings.DEFAULT_MEDIA_PROMPT,
                is_default=True,
                is_active=True,
            )
            db.add(default_prompt)
            db.commit()

    finally:
        db.close()


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """
    Root endpoint - render home page or redirect to dashboard if logged in.
    """
    return templates.TemplateResponse("home.html", {"request": request})


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """
    Dashboard page - will be handled by the API router.
    """
    return RedirectResponse(url="/api/v1/media/dashboard")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """
    Serve favicon.
    """
    return FileResponse("app/static/img/favicon.ico")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
