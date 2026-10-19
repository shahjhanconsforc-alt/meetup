from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response

from backend import sessions
from services.storage import media_storage, minio_client as mc, session_storage

router = APIRouter(prefix="/api/minio", tags=["minio"])


@router.get("/health")
async def health(force: bool = False):
    """Running/stopped, cached for a few seconds.

    The pill polls this. `force=true` skips the cache — for the "check now"
    button, where a stale answer is exactly what the user is trying to escape.
    """
    return mc.health(force=force)


@router.get("/logs")
async def logs(after: int = 0):
    """The MinIO log lines, for the in-app terminal. `after` is the last id the
    client has, so a poll only fetches what's new."""
    return {"logs": mc.recent_logs(after)}


@router.get("/buckets")
async def buckets():
    """Every bucket the app uses, with what's in it."""
    out = []
    for name in mc.ALL_BUCKETS:
        try:
            objects = mc.list_objects(name)
            out.append({"bucket": name, "objects": len(objects),
                        "bytes": sum(o.get("size") or 0 for o in objects)})
        except mc.MinioUnavailable as exc:
            out.append({"bucket": name, "error": str(exc)})
    return {"buckets": out, "endpoint": mc.ENDPOINT}


@router.post("/buckets")
async def create_buckets():
    """Create any missing bucket. Safe to call repeatedly."""
    try:
        return {"buckets": mc.ensure_all_buckets()}
    except mc.MinioUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/objects/{bucket}")
async def objects(bucket: str, prefix: str = ""):
    if bucket not in mc.ALL_BUCKETS:
        # Only this app's buckets. The MinIO credentials may well have access to
        # others on a shared server, and this endpoint is not the place to browse
        # them.
        raise HTTPException(404, f"Unknown bucket {bucket}")
    try:
        return {"objects": mc.list_objects(bucket, prefix)}
    except mc.MinioUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


# ─── Media ───────────────────────────────────────────────────────────────


@router.get("/media")
async def media_list():
    try:
        return {"media": media_storage.listing()}
    except mc.MinioUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/media")
async def media_upload(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file.")
    try:
        return media_storage.put(file.filename or "upload", data,
                                 content_type=file.content_type)
    except mc.MinioUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/media/{name}")
async def media_get(name: str):
    """Serve one stored image, so the picker can show a thumbnail without the
    browser needing credentials for MinIO itself."""
    try:
        data = media_storage.get(name)
    except mc.MinioUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    if data is None:
        raise HTTPException(404, f"No media {name}")
    return Response(content=data,
                    media_type=media_storage.content_type_for(name),
                    headers={"Cache-Control": "max-age=300"})


@router.delete("/media/{name}")
async def media_remove(name: str):
    try:
        return media_storage.remove(name)
    except mc.MinioUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


# ─── Session backups ─────────────────────────────────────────────────────


@router.get("/sessions")
async def session_backups():
    try:
        return {"backups": session_storage.listing()}
    except mc.MinioUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/sessions/{account_id}/backup")
async def session_backup(account_id: int):
    """Snapshot one account's Chrome profile to MinIO.

    So a hard-won login survives a rebuilt container. The profile on disk stays
    the source of truth — this is a copy, not a move.
    """
    try:
        return session_storage.backup(account_id, sessions.profile_dir(account_id))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except mc.MinioUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/sessions/{account_id}/restore")
async def session_restore(account_id: int):
    """Unpack a stored snapshot back over the account's profile.

    Chrome is closed first: restoring under a running process replaces the files
    it has open and corrupts the session rather than restoring it.
    """
    import asyncio

    from backend.automation import playwright_runner as browser

    await browser.close_browser(profile=sessions.profile_dir(account_id))
    await asyncio.sleep(1.2)
    try:
        result = session_storage.restore(account_id, sessions.profile_dir(account_id))
    except mc.MinioUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    if not result["restored"]:
        raise HTTPException(404, f"No backup for account {account_id}")
    return {**result, "session": sessions.refresh(account_id)}


@router.delete("/sessions/{account_id}")
async def session_backup_remove(account_id: int):
    try:
        return session_storage.remove(account_id)
    except mc.MinioUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
