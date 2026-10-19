"""Read handles and message drafts out of uploaded files.

Operators arrive with a list in whatever their last tool exported: a CSV from a
spreadsheet, a TSV from this app's own export, a bare .txt someone pasted
together, or an .xlsx straight out of Excel. Making them convert it first is
work the app can just do.

    parse_handles(name, data)    -> {"usernames": [...], "members": [...], ...}
    parse_messages(name, data)   -> [{"label", "text"}, ...]

Two rules throughout:

* **Never guess silently.** Every result reports which column it read and what
  it skipped, so a file whose "username" column was actually full of email
  addresses is visible before it becomes a campaign.
* **Never raise on bad input.** A malformed file is an ordinary thing an
  operator does, not an exception — it comes back as a result with a `detail`
  saying what went wrong.

.xlsx needs openpyxl. It is imported lazily so a missing dependency disables
spreadsheets rather than breaking the whole upload path.
"""
from __future__ import annotations

import csv
import io
import re

# Extensions this module knows. Anything else is read as plain text, which is
# usually right — a .list or .dat of one handle per line parses fine.
TEXT_EXT = (".txt", ".list", ".dat")
TABLE_EXT = (".csv", ".tsv", ".tab")
EXCEL_EXT = (".xlsx", ".xlsm")

MAX_BYTES = 12 * 1024 * 1024

# Header names that plausibly hold a handle, best first. Checked case- and
# separator-insensitively, so "Member ID" matches "member_id".
_HANDLE_COLUMNS = ("username", "handle", "member", "memberid", "user", "id",
                   "profile", "slug", "name")
_NAME_COLUMNS = ("name", "displayname", "display_name", "fullname", "full_name",
                 "membername", "member_name", "profile_name")
_MESSAGE_COLUMNS = ("text", "message", "body", "content", "draft")


# A Meetup handle is a numeric member id or a vanity slug. Deliberately loose —
# rejecting a real handle is worse than passing through a junk one, which the
# preview shows and the operator can see.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._-]{2,64}$")

# Values that are obviously a header rather than data, when a file has no header
# row that the sniffer recognised.
_NOT_A_HANDLE = {"username", "handle", "member", "user", "id", "name", "email",
                 "profile", "url", "link", "n/a", "na", "none", "null", "-"}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


_TABLE_HINT_COLUMNS = {_norm(col) for col in (*_HANDLE_COLUMNS, *_NAME_COLUMNS,
                                              "event", "bio", "location",
                                              "groups", "used_count")}


def clean_handle(value) -> str:
    """One cell as a handle, or "" if it isn't one.

    A full profile URL is accepted and reduced to its handle: people paste the
    link, not the slug, and refusing that is a pointless conversion step.
    """
    text = str(value or "").strip().strip('"\'').lstrip("@")
    if not text:
        return ""
    if "meetup.com" in text.lower():
        match = re.search(r"/members/([^/?#]+)", text)
        text = match.group(1) if match else ""
    text = text.split("?")[0].strip("/")
    if not text or _norm(text) in _NOT_A_HANDLE:
        return ""
    return text if _HANDLE_RE.match(text) else ""


def _decode(data: bytes) -> str:
    """Bytes to text, tolerating whatever encoding the export used.

    UTF-8 first, then UTF-8 with a BOM (what Excel writes), then latin-1 —
    which cannot fail, so this never raises.
    """
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _rows_from_table(text: str) -> list[list[str]]:
    """Split a CSV/TSV blob into rows.

    The delimiter is sniffed rather than assumed: these files come from
    spreadsheets and exports, and guessing wrong turns every row into one
    column with no error to show for it.
    """
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel_tab if "\t" in sample else csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect) if any(row)]


def _rows_from_excel(data: bytes) -> tuple[list[list[str]], str]:
    """Rows from the first worksheet. Returns ([], reason) if it can't."""
    try:
        import openpyxl
    except ImportError:
        return [], "openpyxl is not installed — save the sheet as CSV instead"
    try:
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        sheet = book[book.sheetnames[0]]
        rows = [["" if c is None else str(c) for c in row]
                for row in sheet.iter_rows(values_only=True)]
        book.close()
        return [r for r in rows if any(v.strip() for v in r)], ""
    except Exception as exc:  # noqa: BLE001 — a corrupt upload is not an error here
        return [], f"could not read the workbook ({type(exc).__name__})"


def _pick_column(header: list[str], wanted: tuple[str, ...]) -> int:
    """Index of the best-matching column, or -1.

    Candidates are tried in PRIORITY order, and each gets both an exact and a
    contains test before the next is considered. Doing all the exact matches
    first would be wrong: on a header of
    `name, city, meetup_username, notes` the weak candidate "name" matches
    exactly and wins, even though "username" matches "meetup_username" and is
    what the file actually meant.
    """
    normalised = [_norm(h) for h in header]
    for candidate in wanted:
        if candidate in normalised:
            return normalised.index(candidate)
        for i, name in enumerate(normalised):
            if candidate and candidate in name:
                return i
    return -1


def _looks_tabular_text(text: str) -> bool:
    """True when a `.txt` blob is probably a tab-separated export.

    Plain handle lists stay line-oriented. MinIO exports, which are stored as
    `.txt` files even though the body is tab-separated, carry tabs and a header
    row naming known columns, so they should be treated as tables instead of as
    a pile of handles.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or "\t" not in text:
        return False
    first = [part.strip() for part in lines[0].split("\t")]
    if len(first) < 2:
        return False
    if any(_norm(cell) in _TABLE_HINT_COLUMNS for cell in first):
        return True
    if len(lines) == 1 and clean_handle(first[0]):
        return True
    return len(lines) > 1 and any("\t" in line for line in lines[1:])


def _table_rows(name: str, data: bytes) -> tuple[list[list[str]], str]:
    lower = name.lower()
    if lower.endswith(EXCEL_EXT):
        return _rows_from_excel(data)
    if lower.endswith(".xls"):
        return [], (".xls is the old binary format — re-save it as .xlsx or CSV")
    return _rows_from_table(_decode(data)), ""


def parse_handles(name: str, data: bytes) -> dict:
    """Pull usernames out of an uploaded file.

    Returns {"usernames", "members", "count", "skipped", "column",
    "name_column", "kind", "detail"}.
    `column` names which column was read so a wrong guess is visible in the
    preview rather than after a campaign has gone out.
    """
    if not data:
        return _empty("the file was empty")
    if len(data) > MAX_BYTES:
        return _empty(f"file is larger than {MAX_BYTES // 1024 // 1024}MB")

    lower = name.lower()
    decoded = _decode(data)
    seen: dict[str, dict[str, str]] = {}
    skipped = 0
    column = ""
    name_column = ""
    kind = "text"

    def add_member(handle: str, member_name: str = "") -> None:
        nonlocal skipped
        cleaned = clean_handle(handle)
        if not cleaned:
            skipped += 1
            return
        key = cleaned.lower()
        row = seen.setdefault(key, {"username": cleaned, "name": ""})
        if member_name and not row["name"]:
            row["name"] = str(member_name).strip()

    if lower.endswith(TEXT_EXT) or ("." not in lower):
        # One handle per line. Commas are tolerated so a pasted "a, b, c" works.
        if _looks_tabular_text(decoded):
            kind = "table"
            rows = [row for row in _rows_from_table(decoded) if any(row)]
            if not rows:
                return _empty("no rows found")
            header = rows[0]
            index = _pick_column(header, _HANDLE_COLUMNS)
            if index >= 0:
                column = header[index]
                body = rows[1:]
            else:
                # No recognisable header: read the first column that actually
                # looks like handles, so a headerless export still works.
                widths = max(len(r) for r in rows)
                index, column, body = 0, "(first column)", rows
                for i in range(widths):
                    hits = sum(1 for r in rows[:25] if i < len(r) and clean_handle(r[i]))
                    if hits >= max(1, min(len(rows), 25) // 2):
                        index, column = i, f"(column {i + 1})"
                        break
            name_index = _pick_column(header, _NAME_COLUMNS)
            if name_index == index:
                name_index = -1
            if name_index >= 0:
                name_column = header[name_index]
            for row in body:
                if index >= len(row):
                    skipped += 1
                    continue
                member_name = row[name_index] if 0 <= name_index < len(row) else ""
                add_member(row[index], member_name)
        else:
            kind = "text"
            for raw_line in decoded.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                # Split ONLY on commas/semicolons, not on whitespace. Splitting
                # on spaces turns a prose line like "not a handle!" into the
                # perfectly valid-looking token "not"; keeping the line whole
                # lets the regex reject it, while "a, b, c" pasted on one line
                # still works.
                pieces = re.split(r"[;,]+", line) if re.search(r"[;,]", line) else [line]
                for piece in pieces:
                    if not piece.strip():
                        continue
                    add_member(piece)
    else:
        kind = "excel" if lower.endswith(EXCEL_EXT) else "table"
        rows, problem = _table_rows(name, data)
        if problem:
            return _empty(problem)
        if not rows:
            return _empty("no rows found")

        header = rows[0]
        index = _pick_column(header, _HANDLE_COLUMNS)
        if index >= 0:
            column = header[index]
            body = rows[1:]
        else:
            # No recognisable header: read the first column that actually looks
            # like handles, so a headerless export still works.
            widths = max(len(r) for r in rows)
            index, column, body = 0, "(first column)", rows
            for i in range(widths):
                hits = sum(1 for r in rows[:25] if i < len(r) and clean_handle(r[i]))
                if hits >= max(1, min(len(rows), 25) // 2):
                    index, column = i, f"(column {i + 1})"
                    break
        name_index = _pick_column(header, _NAME_COLUMNS)
        if name_index == index:
            name_index = -1
        if name_index >= 0:
            name_column = header[name_index]

        for row in body:
            if index >= len(row):
                skipped += 1
                continue
            member_name = row[name_index] if 0 <= name_index < len(row) else ""
            add_member(row[index], member_name)

    members = list(seen.values())
    usernames = [row["username"] for row in members]
    detail = f"{len(usernames)} handle(s) from {name}"
    if column:
        detail += f", column {column!r}"
    if name_column:
        detail += f", name column {name_column!r}"
    if skipped:
        detail += f", {skipped} row(s) skipped"
    return {"usernames": usernames, "members": members, "count": len(usernames),
            "skipped": skipped, "column": column, "name_column": name_column,
            "kind": kind, "detail": detail}


def _empty(detail: str) -> dict:
    return {"usernames": [], "members": [], "count": 0, "skipped": 0,
            "column": "", "name_column": "", "kind": "", "detail": detail}


def parse_messages(name: str, data: bytes) -> dict:
    """Pull message drafts out of an uploaded file.

    Returns {"drafts": [{label, text}], "count", "detail"}.

    A .txt is split on BLANK LINES, not on newlines: campaign messages are
    routinely multi-line, and one-per-line would shred every one of them into
    fragments. A spreadsheet takes one draft per row from a text column.
    """
    if not data:
        return {"drafts": [], "count": 0, "column": "", "detail": "the file was empty"}
    if len(data) > MAX_BYTES:
        return {"drafts": [], "count": 0, "column": "",
                "detail": f"file is larger than {MAX_BYTES // 1024 // 1024}MB"}

    lower = name.lower()
    texts: list[str] = []
    column = ""

    if lower.endswith(TABLE_EXT + EXCEL_EXT) or lower.endswith(".xls"):
        rows, problem = _table_rows(name, data)
        if problem:
            return {"drafts": [], "count": 0, "column": "", "detail": problem}
        if not rows:
            return {"drafts": [], "count": 0, "column": "",
                    "detail": "no rows found"}
        header = rows[0]
        index = _pick_column(header, _MESSAGE_COLUMNS)
        if index >= 0:
            column, body = header[index], rows[1:]
        else:
            # No text column named: take the widest column, since a message is
            # almost always the longest thing on the row.
            widths = max(len(r) for r in rows)
            best, best_len = 0, -1
            for i in range(widths):
                avg = sum(len(r[i]) for r in rows if i < len(r)) / max(1, len(rows))
                if avg > best_len:
                    best, best_len = i, avg
            index, column, body = best, f"(widest column {best + 1})", rows
        texts = [r[index] for r in body if index < len(r) and r[index].strip()]
    else:
        # Blank-line separated blocks — see the docstring.
        blob = _decode(data).replace("\r\n", "\n")
        texts = [block.strip() for block in re.split(r"\n\s*\n", blob)]

    drafts = [{"label": f"msg{i + 1}", "text": text.strip()}
              for i, text in enumerate(texts) if text.strip()]
    detail = f"{len(drafts)} draft(s) from {name}"
    if column:
        detail += f", column {column!r}"
    # `column` is always present, even for a .txt where it is "" — a caller
    # reading result["column"] must not have to know which branch produced it.
    return {"drafts": drafts, "count": len(drafts), "column": column,
            "detail": detail}
