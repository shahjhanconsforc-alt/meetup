"""Campaign user uploads keep names that already live in MinIO exports."""
from __future__ import annotations

from fastapi.testclient import TestClient

from backend import uploads
from backend.main import app


def test_parse_handles_reads_named_rows_from_tab_separated_txt_exports():
    raw = (
        "username\tname\tbio\tlocation\tgroups\tused_count\n"
        "123\tAda Lovelace\tbio\tLondon\tpython-developers-london\t2\n"
        "456\tGrace Hopper\tbio\tNew York\tpython-developers-london\t1\n"
    ).encode("utf-8")

    result = uploads.parse_handles("members.txt", raw)

    assert result["kind"] == "table"
    assert result["count"] == 2
    assert result["column"] == "username"
    assert result["name_column"] == "name"
    assert result["usernames"] == ["123", "456"]
    assert [row["name"] for row in result["members"]] == [
        "Ada Lovelace", "Grace Hopper"]


def test_parse_handles_keeps_plain_txt_handle_lists():
    result = uploads.parse_handles("handles.txt", b"u1\nu2\n")

    assert result["kind"] == "text"
    assert result["usernames"] == ["u1", "u2"]
    assert [row["name"] for row in result["members"]] == ["", ""]


def test_import_users_preserves_names_in_the_member_store():
    with TestClient(app) as client:
        res = client.post("/api/campaign/import/users", json={
            "members": [
                {"username": "123", "name": "Ada Lovelace"},
                {"username": "456", "name": "Grace Hopper"},
            ],
        })
        assert res.status_code == 200
        assert res.json()["added"] == ["123", "456"]

        stored = client.get("/api/data/members").json()["members"]

    assert [row["name"] for row in stored] == [
        "Ada Lovelace", "Grace Hopper"]
