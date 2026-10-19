from fastapi import APIRouter

from backend import settings
from services.storage import cache

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
async def index():
    """Current values plus the schema, so the panel renders itself from one
    response — adding a knob in settings.py is the only change needed to get it
    on screen."""
    return {"settings": settings.current(), "schema": settings.schema(),
            "defaults": settings.defaults()}


@router.post("")
async def update(changes: dict):
    return {"settings": settings.update(changes or {})}


@router.post("/reset")
async def reset():
    return {"settings": settings.reset()}


@router.post("/cache/clear")
async def clear_cache():
    """Drop the store cache.

    For when MinIO was edited from outside the app — the console, a restored
    backup — and the panels are still showing what was cached before that.
    """
    return {"dropped": cache.clear()}


@router.get("/cache")
async def cache_stats():
    return cache.stats()
