"""The JSON store underneath the credentials.

Only what the account/session path depends on: that a store is its own object,
and that clearing data never takes the credentials with it.
"""
from __future__ import annotations

import json

import pytest

from backend import accounts, config, db


def test_a_fresh_store_does_not_share_the_empty_template():
    """Regression: `_EMPTY`'s values are mutable lists.

    A shallow copy handed each new store the module constant's own list, so
    every account/record appended went into `_EMPTY` — and the next store loaded
    without that key started out holding another install's rows.
    """
    accounts.add_account("Tester", "tester@example.com", "pw")
    assert db._EMPTY["accounts"] == []
    assert db._EMPTY["records"] == []
    assert db._EMPTY["runs"] == []


def test_a_store_file_missing_keys_gets_its_own_lists(tmp_path, monkeypatch):
    """The other half of the same bug: `{**_EMPTY, **json.load(...)}` aliased
    the constant for any key the file on disk didn't carry."""
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"records": [], "seq": 0}), encoding="utf-8")
    monkeypatch.setattr(config, "DB_FILE", legacy)
    monkeypatch.setattr(db, "_cache", None)

    db.add_account({"label": "L", "login": "legacy@example.com", "password": "pw"})

    assert db._EMPTY["accounts"] == []


def test_the_store_survives_a_reload(tmp_path, monkeypatch):
    acct = accounts.add_account("Tester", "tester@example.com", "hunter2")
    monkeypatch.setattr(db, "_cache", None)          # as if the process restarted
    reloaded = db.get_account(acct["id"])
    assert reloaded["login"] == "tester@example.com"
    assert reloaded["password"] == "hunter2"


def test_a_corrupt_store_falls_back_to_empty(tmp_path, monkeypatch):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json at all", encoding="utf-8")
    monkeypatch.setattr(config, "DB_FILE", broken)
    monkeypatch.setattr(db, "_cache", None)

    assert db.list_accounts() == []
    assert json.loads(broken.read_text(encoding="utf-8"))["accounts"] == []


def test_clear_keeps_the_accounts(account):
    """"Clear all" on the Data panel must not silently destroy credentials the
    operator typed in."""
    db.insert({"kind": "record"})
    db.clear()
    assert db.get_all() == []
    assert len(db.list_accounts()) == 1


def test_update_account_on_unknown_id_returns_none():
    assert db.update_account(9999, {"logged_in": True}) is None


def test_ids_do_not_repeat_after_a_removal():
    """A reused id would point a live Chrome profile directory (profiles/acct-N)
    at whichever account inherited the number."""
    first = accounts.add_account("A", "a@example.com", "pw")
    accounts.remove_account(first["id"])
    second = accounts.add_account("B", "b@example.com", "pw")
    assert second["id"] != first["id"]
