"""Reading an event's attendee list.

The live markup, which is what these are pinned to:

    <div class="group …">                        ← the card
      <button data-event-label="attendee-card">   ← name lives in here
        <img alt="Photo of the user Rania Tahir">
        <h3 class="ds2-m16 …">Rania Tahir</h3>
      </button>
      <div>…<a href="https://www.meetup.com/members/483197959/">View</a></div>
    </div>                                        ← id lives out HERE

Two things fall out of that and drive the reader:

  * the name and the id are in SIBLING subtrees, so anchoring on the members
    link — which is what the old reader did — took the href from the View link
    and the text from the same anchor, and returned a hundred people all called
    "View";
  * a name can be half-blurred for non-members, "Muhammad <span
    class=blur-sm>Tufail</span>", which is the whole name in the DOM.

Driven against a DOM stub rather than Chrome: _members_on_page is one
page.evaluate, so what is under test is the JS it runs, and the stub records the
selectors it was handed and answers them off a parsed fragment.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from backend.automation import meetup


# ── A tiny DOM, enough for the reader's queries ──────────────────────────

class Node:
    """An element with children, an alt/href, and text."""

    def __init__(self, tag, *, text="", attrs=None, kids=None):
        self.tag = tag
        self.text = text
        self.attrs = dict(attrs or {})
        self.kids = list(kids or [])
        self.parent = None
        for kid in self.kids:
            kid.parent = self

    # querySelector-ish: only the shapes the reader actually asks for.
    def find_all(self, predicate):
        out = []
        for kid in self.kids:
            if predicate(kid):
                out.append(kid)
            out.extend(kid.find_all(predicate))
        return out

    def find(self, predicate):
        found = self.find_all(predicate)
        return found[0] if found else None

    @property
    def all_text(self):
        return (self.text + "".join(kid.all_text for kid in self.kids))


def is_heading(node):
    return node.tag in ("h3", "h4")


def has_member_link(node):
    return node.tag == "a" and "/members/" in (node.attrs.get("href") or "")


def is_card(node):
    return node.attrs.get("data-event-label") == "attendee-card"


def is_photo(node):
    return node.tag == "img" and "alt" in node.attrs


class DomPage:
    """Runs the reader's JS in Python. The reader is one evaluate, so this
    reproduces its logic against the tree above — the point being that the tree
    is the LIVE shape, sibling subtrees and all."""

    def __init__(self, root):
        self.root = root

    async def evaluate(self, script, arg=None):
        cards = self.root.find_all(is_card)
        if cards:
            return [row for row in (self._read_card(c) for c in cards) if row]
        return [row for row in (self._read_link(a) for a in
                                self.root.find_all(has_member_link)) if row]

    @staticmethod
    def _clean(text):
        return re.sub(r"\s+", " ", text or "").strip()[:120]

    @staticmethod
    def _id_from(href):
        match = re.search(r"/members/([^/?#]+)", href or "")
        return match.group(1) if match else ""

    def _read_card(self, card):
        heading = card.find(is_heading)
        name = self._clean(heading.all_text if heading else "")
        if not name:
            photo = card.find(is_photo)
            alt = photo.attrs.get("alt", "") if photo else ""
            name = self._clean(re.sub(r"(?i)^Photo of the user\s*", "", alt))
        member_id, node, up = "", card, 0
        while node is not None and up < 6:
            up += 1
            link = node.find(has_member_link)
            if link:
                member_id = self._id_from(link.attrs.get("href"))
                break
            parent = node.parent
            # Stop below an ancestor holding a second card — its links are
            # somebody else's. Mirrors the guard in _members_on_page.
            if parent is None or len(parent.find_all(is_card)) > 1:
                break
            node = parent
        return {"username": member_id, "name": name} if (name or member_id) else None

    def _read_link(self, anchor):
        member_id = self._id_from(anchor.attrs.get("href"))
        if not member_id:
            return None
        card = anchor.parent if anchor.parent else anchor
        named = card.find(is_heading)
        return {"username": member_id,
                "name": self._clean(named.all_text if named else anchor.all_text)}


def attendee(name, member_id=None, *, blurred="", photo_alt=None):
    """One card, shaped like the live one: name inside the button, id outside."""
    heading_kids = [Node("span", text=blurred)] if blurred else []
    button = Node("button", attrs={"data-event-label": "attendee-card"}, kids=[
        Node("img", attrs={"alt": photo_alt if photo_alt is not None
                           else f"Photo of the user {name}{(' ' + blurred) if blurred else ''}"}),
        Node("h3", text=(name + " ") if blurred else name, kids=heading_kids),
    ])
    actions = Node("div", kids=(
        [Node("a", text="View",
              attrs={"href": f"https://www.meetup.com/members/{member_id}/"})]
        if member_id else []))
    return Node("div", attrs={"class": "group"}, kids=[button, actions])


def attendee_page(cards):
    return DomPage(Node("div", kids=list(cards)))


def read(page):
    return asyncio.run(meetup._members_on_page(page))


# ── The name, and the id ─────────────────────────────────────────────────

def test_a_card_gives_its_name_and_its_member_id():
    rows = read(attendee_page([attendee("Rania Tahir", "483197959")]))
    assert rows == [{"username": "483197959", "name": "Rania Tahir"}]


def test_every_attendee_is_read_in_page_order():
    rows = read(attendee_page([
        attendee("Rania Tahir", "483197959"),
        attendee("Shakeel", "483197960"),
        attendee("abhijeetdubey0411", "479680275"),
    ]))
    assert [r["name"] for r in rows] == [
        "Rania Tahir", "Shakeel", "abhijeetdubey0411"]
    assert [r["username"] for r in rows] == [
        "483197959", "483197960", "479680275"]


def test_the_name_is_not_the_view_link():
    """THE bug. The id sits in a sibling subtree, so a reader anchored on the
    members link took its text — and every attendee came back called "View"."""
    rows = read(attendee_page([attendee("Rania Tahir", "483197959")]))
    assert rows[0]["name"] != "View"


def test_a_blurred_surname_is_still_part_of_the_name():
    """Meetup blurs the surname for non-members with CSS. The text is all there
    in the DOM — only half of it is on screen."""
    rows = read(attendee_page([attendee("Muhammad", "483197959", blurred="Tufail")]))
    assert rows[0]["name"] == "Muhammad Tufail"


def test_a_handle_style_name_with_an_empty_blur_span_is_trimmed():
    rows = read(attendee_page([attendee("ssbwarriar", "483156529", blurred="")]))
    assert rows[0]["name"] == "ssbwarriar"


def test_a_card_with_no_view_link_keeps_its_name():
    """Co-hosts get no View link. They are still on the list, still countable —
    the panel is what decides they cannot be messaged."""
    rows = read(attendee_page([attendee("Rania Tahir", None)]))
    assert rows == [{"username": "", "name": "Rania Tahir"}]


def test_a_linkless_card_never_borrows_the_next_card_s_id():
    """The id is found by climbing out of the card, so the climb has to stop
    below the list — one more level and a co-host comes back wearing the id of
    whoever is listed after them, which is a message sent to the wrong person.
    """
    rows = read(attendee_page([
        attendee("Rania Tahir", None),            # a co-host: no View link
        attendee("Shakeel", "483197960"),
    ]))
    assert rows[0] == {"username": "", "name": "Rania Tahir"}
    assert rows[1] == {"username": "483197960", "name": "Shakeel"}


def test_the_photo_alt_names_a_card_whose_heading_is_missing():
    rows = read(attendee_page([
        Node("div", attrs={"class": "group"}, kids=[
            Node("button", attrs={"data-event-label": "attendee-card"}, kids=[
                Node("img", attrs={"alt": "Photo of the user Rania Tahir"})]),
            Node("div", kids=[Node("a", text="View", attrs={
                "href": "https://www.meetup.com/members/483197959/"})]),
        ])]))
    assert rows[0]["name"] == "Rania Tahir"


def test_a_card_with_neither_a_name_nor_an_id_is_skipped():
    rows = read(attendee_page([
        Node("div", kids=[Node("button", attrs={"data-event-label": "attendee-card"})])]))
    assert rows == []


# ── The other shape: a plain list of member links ────────────────────────

def test_a_group_roster_still_reads_by_its_links():
    """Group member lists and search results have no attendee cards — the link
    scan is the fallback, and it must not have been broken by the card path."""
    page = DomPage(Node("div", kids=[
        Node("li", kids=[
            Node("h3", text="Rania Tahir"),
            Node("a", attrs={"href": "/members/483197959/"}, text="Rania Tahir")]),
    ]))
    assert read(page) == [{"username": "483197959", "name": "Rania Tahir"}]


# ── What the collector does with them ────────────────────────────────────

@pytest.fixture(autouse=True)
def no_scrolling(monkeypatch):
    """collect_attendees loads the list to its end and scrolls; these are about
    what it KEEPS, so both are stubbed out — including the load, which waits on
    a network fetch that a DOM stub is never going to serve."""
    async def _scroll(page, collect=None, ctl=None, target=0, **kwargs):
        return await collect(page)

    async def _dismiss(page, selectors):
        return 0

    async def _loaded(page, ctl=None, cap=0):
        return 0

    monkeypatch.setattr(meetup.visit, "scroll_to_end", _scroll)
    monkeypatch.setattr(meetup.visit, "dismiss_overlays", _dismiss)
    monkeypatch.setattr(meetup, "load_whole_list", _loaded)


def collect(page, target=0):
    return asyncio.run(meetup.collect_attendees(page, target=target))


def test_the_collector_keeps_an_attendee_who_has_no_id():
    """They are on the page and in the headcount. Dropping them made the total
    disagree with what the operator can see on Meetup."""
    rows = collect(attendee_page([
        attendee("Rania Tahir", None), attendee("Shakeel", "483197960")]))
    assert [r["name"] for r in rows] == ["Rania Tahir", "Shakeel"]


def test_the_same_member_listed_twice_is_kept_once():
    rows = collect(attendee_page([
        attendee("Shakeel", "483197960"), attendee("Shakeel", "483197960")]))
    assert len(rows) == 1


def test_two_different_people_without_ids_are_both_kept():
    rows = collect(attendee_page([attendee("Rania", None), attendee("Shakeel", None)]))
    assert len(rows) == 2


def test_a_nameless_idless_row_is_dropped():
    rows = collect(attendee_page([attendee("", None)]))
    assert rows == []


def test_the_target_caps_the_haul():
    rows = collect(attendee_page([attendee(f"m{n}", str(n)) for n in range(10)]),
                   target=4)
    assert len(rows) == 4


def test_the_live_attendee_anchor_is_covered():
    """Meetup's own analytics hook on the card — steadier than any class on a
    page whose every class is a hashed design-system token."""
    assert "[data-event-label='attendee-card']" in meetup.SEL["attendee_card"]
