from fastapi import APIRouter

from app.api.v1.endpoints import media, public

api_router = APIRouter()

# Include routers for different endpoints
api_router.include_router(media.router, prefix="/media", tags=["media"])
api_router.include_router(public.router, prefix="/public", tags=["public"])
