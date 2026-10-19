"""Back up and restore browser sessions to MinIO's sessions bucket.

A logged-in Meetup session lives in a Chrome profile directory on disk
(backend/data/profiles/<account>/). That directory is the real thing — this
module only ships a COPY of it to MinIO so a session survives a rebuilt
container or moves to another machine, instead of every account having to solve
a login challenge again.

One tar.gz object per account, named by account id. Restore refuses to overwrite
a profile Chrome currently has open: the files would be replaced under a running
process and the session would be corrupted rather than restored.

    backup(account_id, profile_dir)     upload a snapshot
    restore(account_id, profile_dir)    download and unpack it
    listing()                           what snapshots exist
    remove(account_id)                  delete one snapshot

Nothing here is on the hot path — sessions are read from disk by
backend/sessions.py, which never touches MinIO.
"""
from __future__ import annotations

import io
import tarfile
from pathlib import Path

from . import minio_client as mc

log = mc.log


def object_name(account_id) -> str:
    return f"profile-{account_id}.tar.gz"


# Chrome's profile holds a large cache that has nothing to do with being logged
# in. Skipping it turns a ~200MB upload into a few MB, and a restored profile
# just re-fetches what it needs.
_SKIP_DIRS = {"Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache",
              "DawnCache", "Service Worker", "Crashpad", "component_crx_cache"}


def _keep(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    return not any(part in _SKIP_DIRS for part in parts)


def backup(account_id, profile_dir) -> dict:
    """Tar up `profile_dir` and store it. Returns {"object", "bytes", "files"}.

    Raises FileNotFoundError if the profile isn't there — backing up a session
    that doesn't exist would write an empty archive that a later restore would
    happily unpack over a good profile.
    """
    root = Path(profile_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"No profile directory at {root}")

    buffer = io.BytesIO()
    files = 0
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or not _keep(path, root):
                continue
            try:
                archive.add(path, arcname=str(path.relative_to(root)))
                files += 1
            except (OSError, PermissionError):
                # Chrome keeps some files locked while it runs. One unreadable
                # file must not lose the whole backup — the cookie store, which
                # is what actually matters, is written and closed on each change.
                continue

    data = buffer.getvalue()
    name = object_name(account_id)
    size = mc.put_bytes(mc.BUCKET_SESSIONS, name, data,
                        content_type="application/gzip")
    log.info("session backup: %s (%d files, %d bytes)", name, files, size)
    return {"object": name, "bytes": size, "files": files}


def restore(account_id, profile_dir) -> dict:
    """Unpack this account's stored snapshot into `profile_dir`.

    Returns {"restored": bool, "files": n}. Missing snapshot is not an error —
    restored is just False. The caller MUST have closed Chrome for this profile
    first; see backend/sessions.py, which does that before calling.
    """
    name = object_name(account_id)
    raw = mc.get_bytes(mc.BUCKET_SESSIONS, name)
    if raw is None:
        log.info("session restore: no snapshot %s", name)
        return {"restored": False, "files": 0}

    root = Path(profile_dir)
    root.mkdir(parents=True, exist_ok=True)
    files = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive.getmembers():
            # Never let an archive write outside the profile: a crafted entry
            # with an absolute path or `..` would otherwise land anywhere on disk.
            target = (root / member.name).resolve()
            if not str(target).startswith(str(root.resolve())):
                log.warning("session restore: skipping unsafe entry %r", member.name)
                continue
            archive.extract(member, path=root)
            files += 1
    log.info("session restore: %s → %s (%d files)", name, root, files)
    return {"restored": True, "files": files}


def listing() -> list[dict]:
    """Every stored snapshot: [{name, size, at}]."""
    return sorted(mc.list_objects(mc.BUCKET_SESSIONS),
                  key=lambda i: i.get("at", 0), reverse=True)


def remove(account_id) -> dict:
    name = object_name(account_id)
    mc.remove_object(mc.BUCKET_SESSIONS, name)
    log.info("session backup removed: %s", name)
    return {"removed": name}
