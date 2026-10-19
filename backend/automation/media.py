"""Attachments — getting an image out of MinIO and into a Meetup composer.

Meetup's message and post composers take a file through a native `<input
type=file>`. Playwright can set that directly, which is the one place where NOT
simulating a human is correct: a real file picker is an OS dialog Playwright
cannot drive at all, so the choice is set_input_files or nothing.

The media itself lives in MinIO (services/storage/media_storage.py). This module
bridges the two: pull the bytes down, write them to a temp file because the
browser needs a path on disk, hand the path over, and clean up.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from backend import activity
from services.storage import media_storage

# Meetup rejects anything larger with a generic error the automation can't read,
# so it's caught here where the reason can actually be reported.
MAX_BYTES = 10 * 1024 * 1024


class MediaError(RuntimeError):
    """The attachment can't be used — wrong type, too big, or not in the bucket."""


def _staging_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "meetup-bot-media"
    path.mkdir(parents=True, exist_ok=True)
    return path


def stage(name: str) -> Path:
    """Download one stored image to a temp file and return its path.

    Raises MediaError with a reason the UI can show, rather than letting a
    missing object surface as a Playwright timeout on the file input.
    """
    key = media_storage.safe_name(name)
    if not media_storage.is_image(key):
        raise MediaError(f"{key} is not an image Meetup will accept")

    data = media_storage.get(key)
    if data is None:
        raise MediaError(f"{key} is not in the media bucket")
    if len(data) > MAX_BYTES:
        raise MediaError(f"{key} is {len(data) // 1024 // 1024}MB — the limit is "
                         f"{MAX_BYTES // 1024 // 1024}MB")

    path = _staging_dir() / key
    path.write_bytes(data)
    return path


def discard(path: Path | None) -> None:
    """Delete a staged file. Never raises — a leftover temp file is not worth
    failing a send that otherwise worked."""
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


async def attach(page, selector: str, name: str) -> dict:
    """Stage `name` and hand it to the file input at `selector`.

    Returns {"attached": name, "bytes": n}. The temp file is removed once the
    browser has read it — set_input_files resolves after the read, so this is
    safe.
    """
    path = stage(name)
    try:
        await page.locator(selector).first.set_input_files(str(path))
        size = path.stat().st_size
        activity.log("browser", f"Attached {path.name} ({size // 1024}KB)", "ok")
        return {"attached": path.name, "bytes": size}
    finally:
        discard(path)


async def attach_many(page, selector: str, names: list[str]) -> list[dict]:
    """Several images through one input, in one call.

    Sequential set_input_files calls REPLACE the selection rather than adding to
    it, so a multi-image attachment has to be staged fully and handed over at
    once — hence the two-phase shape here.
    """
    staged: list[Path] = []
    try:
        for name in names or []:
            staged.append(stage(name))
        if not staged:
            return []
        await page.locator(selector).first.set_input_files([str(p) for p in staged])
        return [{"attached": p.name, "bytes": p.stat().st_size} for p in staged]
    finally:
        for path in staged:
            discard(path)


def listing() -> list[dict]:
    """What the campaign's image picker shows."""
    return [item for item in media_storage.listing() if item.get("is_image")]
