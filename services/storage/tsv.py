"""Tab-separated text codec for the MinIO stores.

Every store here keeps ONE object holding a whole table, and that object is plain
text: a header line naming the columns, then one record per line, columns split by
a TAB. Chosen over JSON because the files are meant to be opened, grepped and
dropped into a spreadsheet.

The catch this module exists to solve: Meetup profile blurbs and group
descriptions are free-form multi-line text — they routinely contain real line
breaks, and plenty contain a `|`. A naive "one record per line" file would split
a single bio across several lines and silently corrupt the table. So every field
is escaped on write — backslash, newline, carriage return and tab become
two-character sequences — and unescaped on read. TAB is the column separator
precisely because no stored bio contains one, while `|` and newlines are common.

    dumps(rows, columns)  -> bytes     header + one escaped line per row
    loads(raw, columns)   -> [dict]    tolerant of column order and extra columns
    looks_like_json(raw)  -> bool      so a store can migrate its old .json body

Round-trip is lossless: loads(dumps(rows)) == rows for string fields.
"""
from __future__ import annotations

SEP = "\t"

# Written form of each character that would otherwise break the line/column shape.
# Backslash MUST be first on the way out, or escaping it would double-escape the
# sequences added after it.
_ESCAPE = ((chr(92), chr(92) * 2), ("\n", chr(92) + "n"),
           ("\r", chr(92) + "r"), ("\t", chr(92) + "t"))
_UNESCAPE = {"n": "\n", "r": "\r", "t": "\t", chr(92): chr(92)}


def escape(value) -> str:
    """One field, safe to sit between two tabs on a single line."""
    text = "" if value is None else str(value)
    for raw, coded in _ESCAPE:
        text = text.replace(raw, coded)
    return text


def unescape(text: str) -> str:
    """Reverse `escape`, in ONE left-to-right pass.

    A pass per sequence would be wrong: unescaping backslash first turns the
    literal two-character `\\n` into a real newline on the following pass, which
    is exactly the corruption this file exists to prevent.
    """
    if chr(92) not in text:
        return text
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == chr(92) and i + 1 < n:
            nxt = text[i + 1]
            if nxt in _UNESCAPE:
                out.append(_UNESCAPE[nxt])
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def dumps(rows, columns: tuple[str, ...]) -> bytes:
    """`rows` as a UTF-8 tab-separated table with a header line."""
    lines = [SEP.join(columns)]
    for row in rows or []:
        lines.append(SEP.join(escape((row or {}).get(c)) for c in columns))
    # Trailing newline so the object ends cleanly and `wc -l` reads naturally.
    return ("\n".join(lines) + "\n").encode("utf-8")


def loads(raw: bytes, columns: tuple[str, ...]) -> list[dict]:
    """Parse a stored table back into dicts.

    The header decides the mapping, so a file whose columns were reordered — or
    which carries a column this build no longer knows — still reads correctly.
    A file with no recognisable header is read positionally against `columns`,
    which is what lets a hand-edited file still load.
    """
    text = raw.decode("utf-8")
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return []

    header = [h.strip() for h in lines[0].split(SEP)]
    if any(h in columns for h in header):
        names, body = header, lines[1:]
    else:
        names, body = list(columns), lines          # headerless: positional

    out: list[dict] = []
    for line in body:
        if not line.strip():
            continue
        parts = line.split(SEP)
        row = {}
        for i, name in enumerate(names):
            if name:
                row[name] = unescape(parts[i]) if i < len(parts) else ""
        out.append(row)
    return out


def looks_like_json(raw: bytes) -> bool:
    """True when this object still holds an old JSON body, so the store knows to
    parse it the legacy way and rewrite it as text."""
    head = raw.lstrip()[:1]
    return head in (b"[", b"{")
