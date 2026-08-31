# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""The wiki publishes the documentation, completely and with live links.

The wiki is generated from `docs/`, so the failure mode is not a wrong
page - it is a *missing* one. A doc added to the repository and forgotten
in `PAGES` is invisible to every reader who arrives via the wiki, and
nothing about the repository looks wrong.

The other failure mode is links: a wiki page namespace is flat and has no
`.md` suffixes, so every inter-doc link has to be rewritten. One that is
missed is a 404 that only a reader hits.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.generate_wiki import (
    PAGES,
    _link_map,
    home_page,
    rewrite_links,
    strip_repo_only_links,
)
from scripts.render_asyncapi import (
    CATEGORIES,
    INDEX_PAGE,
    PUSH_MESSAGE,
    _one_line,
    commands_by_category,
    field_table,
    load_spec,
    main,
    render_command,
    render_index,
    render_page,
    render_push,
    type_cell,
)
from scripts.render_asyncapi import pages as asyncapi_pages

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Docs deliberately not published. Each needs a reason.
NOT_PUBLISHED: dict[str, str] = {}


class TestEveryDocIsPublished:
    def test_no_doc_is_left_out_of_the_wiki(self):
        """A doc added and forgotten here is invisible to wiki readers."""
        on_disk = {p.name for p in (REPO_ROOT / "docs").glob("*.md")}
        published = {Path(src).name for src, _, _ in PAGES}
        missing = sorted(on_disk - published - set(NOT_PUBLISHED))
        assert missing == [], (
            f"docs/ has {', '.join(missing)} but the wiki does not publish them. "
            "Add to PAGES, or to NOT_PUBLISHED with a reason."
        )

    def test_the_wiki_publishes_nothing_that_is_gone(self):
        for source, page, _ in PAGES:
            assert (REPO_ROOT / source).exists(), f"{page} publishes missing {source}"

    def test_every_page_has_a_blurb(self):
        """The index is the landing page; an unexplained entry is dead weight."""
        assert all(blurb.strip() for _, _, blurb in PAGES)

    def test_page_names_are_unique(self):
        names = [page for _, page, _ in PAGES]
        assert len(names) == len(set(names))

    def test_page_names_are_wiki_safe(self):
        """Gitea derives the URL from the name; a space or slash breaks it."""
        for _, page, _ in PAGES:
            assert re.fullmatch(r"[A-Za-z0-9-]+", page), page


class TestLinksSurviveTheMoveToAWiki:
    def test_a_plain_doc_link_becomes_its_page(self):
        assert rewrite_links("[x](protocol.md)", _link_map()) == "[x](Protocol)"

    def test_an_anchor_survives(self):
        """Anchors come from headings, which the page keeps."""
        assert rewrite_links("[x](protocol.md#the-door-clock)", _link_map()) == (
            "[x](Protocol#the-door-clock)"
        )

    def test_an_unknown_target_is_left_alone(self):
        """Better an untouched link than a confidently wrong one."""
        text = "[x](something-else.md)"
        assert rewrite_links(text, _link_map()) == text

    def test_a_link_into_the_source_tree_becomes_text(self):
        """A wiki page has no `src/` beside it, so the link cannot work."""
        assert strip_repo_only_links("[schemas/](schemas/)") == "`schemas/`"
        assert strip_repo_only_links("[base.py](../src/powerpetdoor/x.py)") == "`base.py`"

    def test_ordinary_prose_is_untouched(self):
        text = "See [the docs](https://example.com/x.md) for more."
        assert strip_repo_only_links(text) == text

    @pytest.mark.parametrize("source,_page,_blurb", PAGES, ids=lambda v: str(v))
    def test_no_doc_link_is_left_unrewritten(self, source, _page, _blurb):
        """Every `.md` link in a published doc must resolve to a page."""
        text = (REPO_ROOT / source).read_text(encoding="utf-8")
        rewritten = strip_repo_only_links(rewrite_links(text, _link_map()))
        leftover = {m for m in re.findall(r"\]\(([a-z_]+\.md[^)]*)\)", rewritten)}
        assert leftover == set(), f"{source} still links to {leftover}"


class TestTheIndex:
    def test_it_lists_every_page_it_was_given(self):
        entries = [("Protocol", "the wire"), ("Operation", "the door")]
        home = home_page(entries)
        for page, blurb in entries:
            assert f"[{page}]({page})" in home
            assert blurb in home

    def test_it_says_the_wiki_is_generated(self):
        """Someone will otherwise edit a page and lose the edit."""
        home = home_page([("Protocol", "the wire")])
        assert "generated" in home.lower()
        assert "docs/" in home


class TestTheProtocolReferenceIsRendered:
    """The AsyncAPI document reaches the wiki as pages, not as a filename.

    The spec is generated and complete, but a reader who wanted to know
    what `SET_HOLD_TIME` takes previously had to open 400 KB of JSON. The
    failure mode being pinned here is a command that exists in the spec
    and appears on no page - invisible, with nothing looking wrong.
    """

    @pytest.fixture
    def spec(self):
        return load_spec()

    def test_every_command_lands_on_exactly_one_page(self, spec):
        grouped = commands_by_category(spec)
        placed = [name for names in grouped.values() for name in names]
        expected = sorted(
            name
            for name in spec["components"]["messages"]
            if not name.endswith(".reply") and name != PUSH_MESSAGE
        )
        assert sorted(placed) == expected
        assert len(placed) == len(set(placed)), "a command was filed on two pages"

    def test_every_index_link_resolves_to_a_real_anchor(self, spec):
        """A ToC entry pointing at a heading that does not exist is a 404."""
        index = render_index(spec)
        bodies = {page: render_page(key, spec) for key, page, _, _ in CATEGORIES}
        for page, target in re.findall(r"\]\((Protocol-[A-Za-z-]+)#([a-z_0-9]+)\)", index):
            assert page in bodies, f"index links to unknown page {page}"
            assert f"### {target.upper()}" in bodies[page], f"{page} has no anchor #{target}"

    def test_the_index_lists_every_command(self, spec):
        index = render_index(spec)
        for name in spec["components"]["messages"]:
            if name.endswith(".reply") or name == PUSH_MESSAGE:
                continue
            assert f"`{name}`" in index, f"{name} is missing from the index"

    def test_the_unsolicited_push_is_documented(self, spec):
        """The message a client is likeliest to meet unprepared."""
        assert "DOOR_STATUS (unsolicited)" in render_page("motion", spec)

    def test_object_fields_render_their_members(self, spec):
        """`settings` as `type: object` and nothing else helps nobody."""
        page = render_page("information", spec)
        for member in ("settings.holdOpenTime", "settings.doorOptions", "settings.tz"):
            assert f"`{member}`" in page, f"{member} is not documented"

    def test_an_array_element_gets_its_own_row(self, spec):
        """`schedules` reads as a list of anything without it."""
        page = render_page("schedules", spec)
        assert "| `schedules[]` | integer 0–255 |" in page

    def test_an_array_of_plain_values_adds_no_row(self):
        """Only elements that carry something worth a row get one.

        `daysOfWeek` is seven 0/1 flags already spelled out in the type
        cell; a `daysOfWeek[]` row beneath it would be noise.
        """
        payload = {
            "properties": {
                "d": {"type": "array", "items": {"type": "integer", "enum": [0, 1]}},
            }
        }
        assert not any("d[]" in row for row in field_table(payload))

    def test_units_reach_the_table(self, spec):
        """A reader who misses `centiseconds` sends seconds and is 100x off."""
        assert "(centiseconds)" in render_page("settings", spec)

    def test_the_reference_and_the_prose_link_to_each_other(self):
        protocol = (REPO_ROOT / "docs" / "protocol.md").read_text(encoding="utf-8")
        assert "Protocol-Reference" in protocol, "protocol.md does not link to the reference"
        assert "[Protocol](Protocol)" in render_index(load_spec())

    def test_a_command_with_no_reply_says_so(self):
        """An empty heading makes a reader guess; a sentence does not."""
        spec = {"components": {"messages": {"X": {"payload": {}, "description": "d"}}}}
        assert "answers with the envelope only" in "\n".join(render_command("X", spec))


class TestTheTypeColumnCarriesWhatConstrains:
    """The type cell is where a reader learns the shape at a glance."""

    def test_a_const_is_shown_as_its_value(self):
        assert type_cell({"const": "p2d"}) == '`"p2d"`'

    def test_an_enum_lists_its_members(self):
        assert type_cell({"enum": ["true", "false"]}) == '`"true"` \\| `"false"`'

    def test_a_range_is_shown(self):
        assert type_cell({"type": "integer", "minimum": 0, "maximum": 90000}) == "integer 0–90000"

    def test_a_one_sided_range_is_shown(self):
        assert type_cell({"type": "integer", "minimum": 0}) == "integer ≥ 0"
        assert type_cell({"type": "integer", "maximum": 7}) == "integer ≤ 7"

    def test_a_unit_is_shown(self):
        assert type_cell({"type": "integer", "x-unit": "millivolts"}) == "integer (millivolts)"

    def test_a_fixed_length_array_says_so(self):
        cell = type_cell(
            {"type": "array", "items": {"type": "integer"}, "minItems": 7, "maxItems": 7}
        )
        assert cell == "array of integer, exactly 7"

    def test_a_variable_array_does_not_claim_a_length(self):
        assert type_cell({"type": "array", "items": {"type": "integer"}}) == "array of integer"

    def test_an_untyped_field_is_not_invented(self):
        assert type_cell({}) == "any"

    def test_a_pipe_in_a_description_cannot_break_the_table(self):
        assert _one_line("a | b") == "a \\| b"

    def test_a_wrapped_description_becomes_one_line(self):
        assert _one_line("a\n  b\n\nc") == "a b c"


class TestThePageSet:
    """`pages()` is what the wiki generator actually calls."""

    def test_it_returns_the_index_first(self):
        rendered = asyncapi_pages(load_spec())
        assert rendered[0][0] == INDEX_PAGE

    def test_it_returns_a_page_per_category(self):
        names = [page for page, _, _ in asyncapi_pages(load_spec())]
        assert names == [INDEX_PAGE] + [page for _, page, _, _ in CATEGORIES]

    def test_every_page_has_a_blurb_and_a_body(self):
        for page, blurb, body in asyncapi_pages(load_spec()):
            assert blurb.strip(), f"{page} has no blurb"
            assert body.startswith("# "), f"{page} does not open with a heading"
            assert body.endswith("\n")

    def test_a_command_without_prose_still_renders(self):
        """The spec always has descriptions; the renderer must not require it.

        A message added to the document before its `COMMAND_DOCS` entry
        would otherwise crash the wiki build rather than render thinly.
        """
        spec = {"components": {"messages": {"X": {"payload": {}}}}}
        assert "### X" in "\n".join(render_command("X", spec))

    def test_a_command_without_tags_omits_the_tag_line(self):
        spec = {"components": {"messages": {"X": {"payload": {}, "description": "d"}}}}
        assert "Tags:" not in "\n".join(render_command("X", spec))

    def test_the_push_renders_without_prose(self):
        spec = {"components": {"messages": {PUSH_MESSAGE: {"payload": {}}}}}
        assert "DOOR_STATUS (unsolicited)" in "\n".join(render_push(spec))

    def test_the_command_line_entry_point_lists_the_pages(self, capsys):
        assert main() == 0
        out = capsys.readouterr().out
        assert INDEX_PAGE in out
        assert "Protocol-Settings" in out
