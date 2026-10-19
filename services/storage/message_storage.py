"""Campaign message drafts, kept in MinIO's messages bucket.

The drafts behind the campaign's msg1/msg2/… tabs — the text the bot sends
Meetup members, plus whichever placeholders each one uses. One JSON object,
because unlike the member tables these are a handful of records that nobody
greps and that carry deliberate multi-line formatting.

The app reads and writes through backend/db.py's get_message_drafts /
save_message_drafts, which keep an in-memory copy so a brief MinIO outage
degrades to "you can still see your drafts" rather than an error page.

    load()        every draft, in order
    push(drafts)  replace the whole set
"""
from __future__ import annotations

import json

from . import minio_client as mc

OBJECT_NAME = "message_drafts.json"

log = mc.log

# Placeholders a draft may use; substituted per-recipient at send time. Listed
# here (not in the campaign module) because this is the file that defines what a
# draft IS — the campaign only fills them in.
PLACEHOLDERS = ("{name}", "{first_name}", "{group}", "{city}", "{category}")


def _clean(draft, index: int) -> dict | None:
    """One stored draft. Accepts a bare string (an early build stored those) or a
    {label, text, disabled} record, and always returns the record form.

    `disabled` is kept because a draft turned off is not a draft deleted: the
    text is still worth having next month, and the alternative — delete it and
    paste it back later — is how a campaign loses the line that worked.
    """
    if isinstance(draft, str):
        text = draft
        label = f"msg{index + 1}"
        disabled = False
    elif isinstance(draft, dict):
        text = str(draft.get("text") or "")
        label = str(draft.get("label") or "").strip() or f"msg{index + 1}"
        disabled = bool(draft.get("disabled"))
    else:
        return None
    if not text.strip():
        return None
    return {"label": label, "text": text, "disabled": disabled}


def load() -> list[dict]:
    """Every stored draft. Empty list if none yet. Raises MinioUnavailable when
    the server is down, so a caller can serve its cached copy instead of showing
    an empty editor that a save would then make true."""
    raw = mc.get_bytes(mc.BUCKET_MESSAGES, OBJECT_NAME)
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        log.warning("%s is not valid JSON — treating as empty", OBJECT_NAME)
        return []
    if isinstance(data, dict):
        data = data.get("drafts") or []
    drafts = [c for c in (_clean(d, i) for i, d in enumerate(data or [])) if c]
    log.info("message drafts: loaded %d", len(drafts))
    return drafts


def push(drafts) -> int:
    """Replace the whole draft set. Returns how many were written."""
    cleaned = [c for c in (_clean(d, i) for i, d in enumerate(drafts or [])) if c]
    body = json.dumps(cleaned, indent=2, ensure_ascii=False).encode("utf-8")
    mc.put_bytes(mc.BUCKET_MESSAGES, OBJECT_NAME, body,
                 content_type="application/json; charset=utf-8")
    log.info("message drafts: wrote %d", len(cleaned))
    return len(cleaned)


def count() -> int:
    return len(load())
