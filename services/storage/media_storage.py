"""Images and other media in MinIO's media bucket.

What a campaign attaches to a message, and what the debug screenshots land in.
Binary blobs, one object each — no table, no TSV.

Objects are stored under a flat namespace with their original filename, so what
an operator uploaded is what they see listed. A repeat upload of the same name
overwrites: media here is addressed by name on purpose, so replacing a campaign's
image doesn't mean hunting down a generated id.

    put(name, data, content_type)   store one blob
    get(name)                       read it back (None if absent)
    listing()                       [{name, size, at}] for the picker
    remove(name)                    delete one
"""
from __future__ import annotations

import posixpath
import re

from . import minio_client as mc

log = mc.log

# What a browser will actually render inline. An upload outside this set is still
# stored — the campaign just can't attach it to a Meetup message, which only
# accepts images.
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(name: str) -> str:
    """A filename that can't escape the bucket's flat namespace.

    Any path component is dropped and the rest is reduced to a conservative
    character set — an object name carrying `../` or a raw slash would silently
    land somewhere other than where the listing says it is.
    """
    base = posixpath.basename(str(name or "").replace("\\", "/")).strip()
    cleaned = _SAFE.sub("-", base).strip("-.")
    return cleaned or "upload"


def content_type_for(name: str) -> str:
    ext = posixpath.splitext(safe_name(name).lower())[1]
    return IMAGE_TYPES.get(ext, "application/octet-stream")


def is_image(name: str) -> bool:
    return posixpath.splitext(safe_name(name).lower())[1] in IMAGE_TYPES


def put(name: str, data: bytes, content_type: str | None = None) -> dict:
    """Store one blob. Returns {name, size, content_type} — `name` is the
    sanitized one actually used, which may differ from what was passed."""
    key = safe_name(name)
    ctype = content_type or content_type_for(key)
    size = mc.put_bytes(mc.BUCKET_MEDIA, key, data, content_type=ctype)
    log.info("media: stored %r (%d bytes, %s)", key, size, ctype)
    return {"name": key, "size": size, "content_type": ctype}


def get(name: str) -> bytes | None:
    """Read one blob back. None when it isn't there; raises only if MinIO is
    down, so 'missing' and 'unreachable' never get confused."""
    return mc.get_bytes(mc.BUCKET_MEDIA, safe_name(name))


def listing() -> list[dict]:
    """Everything in the media bucket, newest first — what the picker shows."""
    items = mc.list_objects(mc.BUCKET_MEDIA)
    for item in items:
        item["is_image"] = is_image(item["name"])
    return sorted(items, key=lambda i: i.get("at", 0), reverse=True)


def remove(name: str) -> dict:
    key = safe_name(name)
    mc.remove_object(mc.BUCKET_MEDIA, key)
    log.info("media: removed %r", key)
    return {"removed": key}


def count() -> int:
    return len(mc.list_objects(mc.BUCKET_MEDIA))
