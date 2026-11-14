"""Turn the PostgreSQL 16 release notes (appendix E of the documentation) into chunks.

The appendix index page (``release.html``) lists one page per release:
``release-16.html`` for 16.0 and ``release-16-N.html`` for each minor release.
``parse_release_index`` reads that list; file names are never guessed.

Page structure
--------------
Minor release (``release-16-3.html``)::

    E.14. Release 16.3            Release date, one boilerplate sentence
      E.14.1. Migration to Version 16.3     prose only
      E.14.2. Changes                       one flat list of items

Major release (``release-16.html``)::

    E.17. Release 16              Release date
      E.17.1. Overview                      lead-in, short highlight list
      E.17.2. Migration to Version 16       prose, then a list of incompatibilities
      E.17.3. Changes                       lead-in sentence
        E.17.3.1. Server
          E.17.3.1.1. Optimizer             list of items
          ...
        E.17.3.2. Logical Replication       list of items
        ...
      E.17.4. Acknowledgments               list of names

Chunks (``meta.chunk_type``)
----------------------------
``items``
    One release-note item (an ``<li>`` of an itemized list: its summary
    paragraph plus any explanatory paragraphs, code listings and procedure
    steps), or a small group of adjacent items. An item of at least
    ``min_item_chars`` characters is a chunk of its own. Shorter neighbours in
    the same section are packed, in order, into groups of at most ``max_chars``
    characters and ``MAX_GROUP_ITEMS`` items. A group never crosses a section
    boundary or a long item. An
    item is never split unless it is longer than ``ITEM_SPLIT_CHARS``.
    Paragraphs of one item are separated by a line break, items by a blank line.
``prose``
    A section's own text outside its lists: "Migration to Version X" (for the
    major release its incompatibility items follow as ``items`` chunks of the
    same section).
``overview``
    The major release's "Overview" section as one chunk, highlights as bullets.

Left out: the commit links ("§"), which go to ``meta.commits``; the author
credit that ends every item summary, which goes to ``meta.authors``; the
"Release date" line (``meta.release_date``); two boilerplate sentences (see
``BOILERPLATE_PROSE``); the "Acknowledgments" list of names; navigation,
tables of contents and heading permalinks. Section numbers ("E.14.2.") are kept
out of titles and ids because they change with every new minor release; they
are in ``meta.section_number``.

Chunk ids: ``relnotes:<version>#<section>`` for prose and overview chunks and
``relnotes:<version>#<section>:i<N>`` or ``...:i<N>-<M>`` for items, where
``<section>`` is the section's HTML id without the release prefix, lower-cased
(``RELEASE-16-3-CHANGES`` -> ``changes``, ``RELEASE-16-OPTIMIZER`` ->
``optimizer``) and N, M count the items of that section from 1. Examples:
``relnotes:16.3#changes:i12``, ``relnotes:16.0#optimizer:i1-6``,
``relnotes:16.3#migration``.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from pgdiag.chunks import ID_PREFIXES, Chunk, find_settings
from pgdiag.fetch import normalize_url
from pgdiag.ingest.docs.crawl import DOCS_BASE_URL, PG_VERSION, page_name
from pgdiag.ingest.docs.parse import (
    BLOCK_JOINER,
    BLOCK_TAGS,
    BOILERPLATE_SELECTORS,
    HEADING_TAGS,
    TITLE_SEPARATOR,
    clean,
    split_into_parts,
)

SOURCE = "release_notes"
ID_PREFIX = ID_PREFIXES[SOURCE]
INDEX_URL = DOCS_BASE_URL + "release.html"

DEFAULT_MAX_CHARS = 1000  # a group of short items holds at most this many characters ...
MAX_GROUP_ITEMS = 6  # ... and at most this many items
DEFAULT_MIN_ITEM_CHARS = 250  # an item at least this long is a chunk of its own
ITEM_SPLIT_CHARS = 3000  # only an item longer than this is split ...
SPLIT_PART_CHARS = 1800  # ... into parts of about this size
ITEM_JOINER = "\n"  # between the paragraphs of one item

COMMIT_LINK_TEXT = "§"
SECTION_DIV_CLASSES: frozenset[str] = frozenset({"sect1", "sect2", "sect3", "sect4", "sect5"})

#: "E.14.2. Changes" -> ("E.14.2", "Changes")
_HEADING_RE = re.compile(r"^((?:[A-Z]|\d+)(?:\.\d+)*)\.\s+(.+)$")
_RELEASE_NAME_RE = re.compile(r"^Release (\d+)(?:\.(\d+))?$")
_RELEASE_DATE_RE = re.compile(r"^Release date:\s*(.*)$")
_XREF_LABEL_RE = re.compile(r"^(?:Section|Chapter|Appendix|Part|Table|Figure|Example) [\w.]+$")
#: The author credit at the end of an item summary: "... (Tom Lane, Jeff Davis)". The
#: space before it tells it apart from an argument list ("pg_size_pretty(numeric)").
_CREDIT_RE = re.compile(r"\s+\(([^()\d]+)\)$")
_CREDIT_CONNECTIVE_RE = re.compile(r"^(?:(?:and|with|per|by|hat tip to|help from|thanks to)\s+)+")
_OVERVIEW_RE = re.compile(r"^Overview$", re.IGNORECASE)
_ACKNOWLEDGMENTS_RE = re.compile(r"^Acknowledge?ments?$", re.IGNORECASE)

#: Paragraphs that say nothing a reader of a single chunk could use. Anything
#: else outside the lists is kept in a ``prose`` chunk.
BOILERPLATE_PROSE: tuple[re.Pattern[str], ...] = (
    re.compile(r"Release date:.*"),
    re.compile(
        r"This release contains .{0,80}? from \d+(?:\.\d+)*\. "
        r"For information about new features in major release \d+, see .{0,80}"
    ),
    re.compile(
        r"Below you will find a detailed account of the changes between .{0,80} "
        r"and the previous major release\."
    ),
)


class ReleaseNotesError(ValueError):
    """A release-notes page does not have the expected structure."""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def split_heading(heading: str) -> tuple[str | None, str]:
    """``"E.14.2. Changes"`` -> ``("E.14.2", "Changes")``; no number -> ``(None, heading)``."""
    heading = clean(heading)
    match = _HEADING_RE.match(heading)
    return (match.group(1), match.group(2)) if match else (None, heading)


def release_version(name: str) -> str | None:
    """``"Release 16.3"`` -> ``"16.3"``, ``"Release 16"`` -> ``"16.0"``, anything else -> None."""
    match = _RELEASE_NAME_RE.match(clean(name))
    return f"{match.group(1)}.{int(match.group(2) or 0)}" if match else None


def version_sort_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def is_boilerplate(text: str) -> bool:
    return any(pattern.fullmatch(text) for pattern in BOILERPLATE_PROSE)


def split_credit(summary: str) -> tuple[str, list[str]]:
    """Separate an item summary from the author credit that ends it.

    ``"Fix foo in bar (Tom Lane, Jeff Davis)"`` -> ``("Fix foo in bar", ["Tom Lane",
    "Jeff Davis"])``. A summary without a trailing parenthesis is returned
    unchanged, and so is one whose parenthesis contains a digit or another
    parenthesis, or follows a word directly (a version, a CVE number, a
    function's argument list: not names).
    """
    match = _CREDIT_RE.search(summary)
    if match is None or not summary[: match.start()].strip():
        return summary, []
    authors: list[str] = []
    for part in match.group(1).split(","):
        name = _CREDIT_CONNECTIVE_RE.sub("", part.strip()).strip()
        if name and name not in authors:
            authors.append(name)
    return summary[: match.start()].rstrip(), authors


# --------------------------------------------------------------------------
# The appendix index: which release pages exist
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseRef:
    version: str  # "16.0" for the major release, "16.N" for minor releases
    url: str
    page: str  # "release-16-3"
    heading: str  # "Release 16.3"


@dataclass
class ReleaseIndex:
    releases: list[ReleaseRef]  # oldest first: 16.0, 16.1, ...
    ignored: list[tuple[str, str]] = field(default_factory=list)  # (url, heading): not 16.x notes


def parse_release_index(html: str | bytes, url: str = INDEX_URL) -> ReleaseIndex:
    """The PostgreSQL 16 release pages listed in the appendix's table of contents.

    Entries that are not a PostgreSQL 16 release ("Prior Releases", a pointer to
    the notes of older branches) are returned in ``ignored``. Raises
    ``ReleaseNotesError`` unless 16.0 and an unbroken run of minor releases are found.
    """
    soup = BeautifulSoup(html, "lxml")
    content = soup.find(id="docContent") or soup
    toc = content.find("div", class_="toc")
    if toc is None:
        raise ReleaseNotesError(f"{url}: no table of contents")
    releases: dict[str, ReleaseRef] = {}
    ignored: list[tuple[str, str]] = []
    for anchor in toc.select("span.sect1 > a[href]"):
        _, name = split_heading(anchor.get_text())
        target = normalize_url(urljoin(url, anchor["href"]))
        version = release_version(name)
        page = page_name(target)
        if version is None or page is None or version.split(".")[0] != PG_VERSION:
            ignored.append((target, name))
            continue
        if version in releases:
            raise ReleaseNotesError(f"{url}: release {version} is listed twice")
        releases[version] = ReleaseRef(version=version, url=target, page=page, heading=name)
    ordered = sorted(releases.values(), key=lambda ref: version_sort_key(ref.version))
    found = [ref.version for ref in ordered]
    expected = [f"{PG_VERSION}.{minor}" for minor in range(len(found))]
    if not found or found != expected:
        raise ReleaseNotesError(
            f"{url}: expected releases {PG_VERSION}.0 to {PG_VERSION}.N without gaps, "
            f"found {found or 'none'}"
        )
    return ReleaseIndex(releases=ordered, ignored=ignored)


# --------------------------------------------------------------------------
# Items and grouping
# --------------------------------------------------------------------------


@dataclass
class Item:
    index: int  # position in its section, from 1
    blocks: list[str]  # summary first, then explanatory paragraphs, listings, steps
    authors: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return ITEM_JOINER.join(self.blocks)


def group_items(
    items: list[Item],
    max_chars: int = DEFAULT_MAX_CHARS,
    min_item_chars: int = DEFAULT_MIN_ITEM_CHARS,
    max_items: int = MAX_GROUP_ITEMS,
) -> list[list[Item]]:
    """Pack adjacent short items into groups; every longer item stays alone.

    Order is preserved and every item ends up in exactly one group. A group
    holds only items shorter than ``min_item_chars``, at most ``max_items`` of
    them, and its joined text is at most ``max_chars`` long.
    """
    groups: list[list[Item]] = []
    pending: list[Item] = []
    size = 0
    for item in items:
        length = len(item.text)
        if length >= min_item_chars:
            if pending:
                groups.append(pending)
                pending, size = [], 0
            groups.append([item])
            continue
        added = length + (len(BLOCK_JOINER) if pending else 0)
        if pending and (size + added > max_chars or len(pending) >= max_items):
            groups.append(pending)
            pending, size, added = [], 0, length
        pending.append(item)
        size += added
    if pending:
        groups.append(pending)
    return groups


# --------------------------------------------------------------------------
# A release page
# --------------------------------------------------------------------------


@dataclass
class Release:
    version: str
    release_date: str | None  # ISO date
    url: str
    page: str
    heading: str  # "Release 16.3"
    chunks: list[Chunk]
    items: int  # release-note items found (before grouping)


@dataclass
class _Section:
    name: str  # heading without its number
    number: str | None  # "E.14.2"
    path: list[str]  # names from below the release heading down to this section
    anchor: str  # for the chunk id
    fragment: str | None  # the element's HTML id, for the URL


class _ReleasePageParser:
    def __init__(
        self,
        html: str | bytes,
        url: str,
        names: Iterable[str] | None,
        max_chars: int,
        min_item_chars: int,
    ) -> None:
        self.url = normalize_url(url)
        page = page_name(self.url)
        if page is None:
            raise ReleaseNotesError(f"not a PostgreSQL {PG_VERSION} documentation page URL: {url}")
        self.page = page
        self.names = tuple(names) if names is not None else None
        self.max_chars = max_chars
        self.min_item_chars = min_item_chars
        soup = BeautifulSoup(html, "lxml")
        content = soup.find(id="docContent")
        if content is None:
            raise ReleaseNotesError(f"{self.url}: no documentation content (id docContent)")
        for selector in BOILERPLATE_SELECTORS:
            for element in content.select(selector):
                element.decompose()
        self._expand_cross_references(content)
        root = content.find("div", class_="sect1")
        if root is None:
            raise ReleaseNotesError(f"{self.url}: no release section (div.sect1)")
        self.root = root
        self.root_id: str | None = root.get("id") or None
        self.number, self.heading = self._heading(root)
        version = release_version(self.heading)
        if version is None:
            raise ReleaseNotesError(
                f"{self.url}: heading {self.heading!r} is not 'Release <version>'"
            )
        self.version = version
        self.release_date = self._release_date()
        self.product = (
            f"PostgreSQL {version.split('.')[0] if version.endswith('.0') else version}"
            " release notes"
        )
        self._anchors: set[str] = set()
        self.items = 0
        self.chunks: list[Chunk] = []

    # ---- page-level -------------------------------------------------------

    @staticmethod
    def _expand_cross_references(content: Tag) -> None:
        """``Section E.15`` -> ``Section E.15 (Release 16.2)``: section numbers alone say
        nothing outside the documentation, and the release sections are renumbered
        with every new minor release."""
        for anchor in content.select("a.xref[title]"):
            if not _XREF_LABEL_RE.match(clean(anchor.get_text())):
                continue
            number, name = split_heading(anchor["title"])
            if number is not None:
                anchor.append(f" ({name})")

    @staticmethod
    def _heading(node: Tag) -> tuple[str | None, str]:
        titlepage = node.find("div", class_="titlepage", recursive=False)
        heading = titlepage.find(HEADING_TAGS) if titlepage is not None else None
        if heading is None:
            heading = node.find(HEADING_TAGS, recursive=False)
        return split_heading(heading.get_text()) if heading is not None else (None, "")

    def _release_date(self) -> str | None:
        for paragraph in self.root.find_all("p", recursive=False):
            match = _RELEASE_DATE_RE.match(clean(paragraph.get_text()))
            if match is None:
                continue
            try:
                return dt.date.fromisoformat(match.group(1)).isoformat()
            except ValueError as exc:
                raise ReleaseNotesError(
                    f"{self.url}: release date {match.group(1)!r} is not an ISO date"
                ) from exc
        return None

    def parse(self) -> Release:
        self._walk(self.root, path=None)
        if self.items == 0:
            raise ReleaseNotesError(f"{self.url}: no release-note items found")
        return Release(
            version=self.version,
            release_date=self.release_date,
            url=self.url,
            page=self.page,
            heading=self.heading,
            chunks=self.chunks,
            items=self.items,
        )

    # ---- sections ---------------------------------------------------------

    @staticmethod
    def _is_section(tag: Tag) -> bool:
        return tag.name == "div" and bool(SECTION_DIV_CLASSES.intersection(tag.get("class") or []))

    def _new_anchor(self, node: Tag, name: str) -> str:
        element_id = node.get("id") or ""
        prefix = f"{self.root_id}-" if self.root_id else None
        if prefix and element_id.startswith(prefix) and len(element_id) > len(prefix):
            anchor = element_id[len(prefix) :].lower()
        else:
            anchor = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "section"
        candidate, n = anchor, 1
        while candidate in self._anchors:
            n += 1
            candidate = f"{anchor}~{n}"
        self._anchors.add(candidate)
        return candidate

    def _walk(self, node: Tag, path: list[str] | None) -> None:
        """``path=None`` marks the release section itself (the page's ``sect1``)."""
        if path is None:
            self._anchors.add("intro")
            section = _Section(self.heading, self.number, [], "intro", None)
        else:
            number, name = self._heading(node)
            if _ACKNOWLEDGMENTS_RE.match(name):
                return
            section = _Section(
                name, number, [*path, name], self._new_anchor(node, name), node.get("id") or None
            )
            if _OVERVIEW_RE.match(name):
                self._discard_commit_links(node)
                self._emit_text(section, "overview", self._blocks(node, top=True))
                return

        prose: list[str] = []
        items: list[Item] = []
        subsections: list[Tag] = []
        for child in node.children:
            if isinstance(child, NavigableString):
                if not isinstance(child, Comment) and clean(str(child)):
                    prose.append(clean(str(child)))
                continue
            if not isinstance(child, Tag):
                continue
            classes = set(child.get("class") or [])
            if child.name == "div" and "titlepage" in classes:
                continue
            if self._is_section(child):
                subsections.append(child)
            elif child.name == "div" and "itemizedlist" in classes:
                for listing in child.find_all(["ul", "ol"], recursive=False):
                    for li in listing.find_all("li", recursive=False):
                        item = self._item(li, len(items) + 1)
                        if item is not None:
                            items.append(item)
            else:
                self._discard_commit_links(child)
                prose.extend(self._blocks(child, top=True))

        prose = [block for block in prose if not is_boilerplate(block)]
        if prose:
            self._emit_text(section, "prose", prose)
        self.items += len(items)
        self._emit_items(section, items)
        for child in subsections:
            self._walk(child, section.path)

    # ---- text -------------------------------------------------------------

    @staticmethod
    def _commit_links(node: Tag) -> list[Tag]:
        return [a for a in node.find_all("a") if clean(a.get_text()) == COMMIT_LINK_TEXT]

    def _discard_commit_links(self, node: Tag) -> None:
        for anchor in self._commit_links(node):
            anchor.decompose()

    def _item(self, li: Tag, index: int) -> Item | None:
        commits: list[str] = []
        for anchor in self._commit_links(li):
            href = anchor.get("href") or ""
            if href.startswith("https://") and href not in commits:
                commits.append(href)
            anchor.decompose()
        first = next((child for child in li.children if isinstance(child, Tag)), None)
        blocks = self._blocks(li)
        if not blocks:
            return None
        authors: list[str] = []
        if first is not None and first.name == "p":
            blocks[0], authors = split_credit(blocks[0])
        return Item(index=index, blocks=blocks, authors=authors, commits=commits)

    def _blocks(self, node: Tag, *, top: bool = False) -> list[str]:
        """Flatten ``node`` into text blocks: paragraphs, code listings, list entries.

        With ``top=True`` the node itself is rendered (a ``<p>`` or list that
        is a direct child of a section) instead of only its children.
        """
        if top and node.name in BLOCK_TAGS and node.name != "div":
            holder = [node]
        else:
            holder = list(node.children)
        out: list[str] = []
        inline: list[str] = []

        def flush() -> None:
            text = clean("".join(inline))
            inline.clear()
            if text:
                out.append(text)

        for child in holder:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                inline.append(str(child))
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name
            if name not in BLOCK_TAGS:
                inline.append("\n" if name == "br" else child.get_text())
                continue
            flush()
            classes = set(child.get("class") or [])
            if name == "div" and "titlepage" in classes:
                continue
            if name == "pre":
                text = child.get_text().strip("\n").rstrip()
                if text.strip():
                    out.append(text)
            elif name in ("ul", "ol"):
                entries = []
                for number, entry in enumerate(child.find_all("li", recursive=False), start=1):
                    body = ITEM_JOINER.join(self._blocks(entry))
                    if body:
                        entries.append((f"{number}. " if name == "ol" else "- ") + body)
                if entries:
                    out.append("\n".join(entries))
            elif name == "table":
                rows = [
                    " | ".join(clean(cell.get_text()) for cell in row.find_all(["th", "td"]))
                    for row in child.find_all("tr")
                ]
                out.extend(row for row in rows if row.strip(" |"))
            elif name == "hr":
                continue
            elif name == "p" or name in HEADING_TAGS:
                text = clean(child.get_text())
                if text:
                    out.append(text)
            else:  # div, blockquote, dl, ...: a transparent container
                out.extend(self._blocks(child))
        flush()
        return out

    # ---- chunks -----------------------------------------------------------

    def _meta(self, section: _Section, chunk_type: str) -> dict:
        return {
            "chunk_type": chunk_type,
            "version": self.version,
            "pg_major": self.version.split(".")[0],
            "pg_version": PG_VERSION,
            "release_kind": "major" if self.version.endswith(".0") else "minor",
            "release_date": self.release_date,
            "page": self.page,
            "page_title": self.heading,
            "section": section.name,
            "section_path": list(section.path),
            "section_id": section.fragment,
            "section_number": section.number,
        }

    def _chunk(self, section: _Section, suffix: str, parts: list[str], meta: dict) -> None:
        title = TITLE_SEPARATOR.join([self.product, *section.path])
        url = f"{self.url}#{section.fragment}" if section.fragment else self.url
        many = len(parts) > 1
        for number, text in enumerate(parts, start=1):
            self.chunks.append(
                Chunk(
                    chunk_id=f"{ID_PREFIX}:{self.version}#{section.anchor}{suffix}"
                    + (f":p{number}" if many else ""),
                    source=SOURCE,
                    url=url,
                    title=title + (f" (part {number}/{len(parts)})" if many else ""),
                    text=text,
                    settings=find_settings(text, self.names),
                    meta={**meta, "part": number, "parts": len(parts)},
                )
            )

    def _emit_text(self, section: _Section, chunk_type: str, blocks: list[str]) -> None:
        parts = split_into_parts(blocks, SPLIT_PART_CHARS)
        if parts:
            self._chunk(section, "", parts, self._meta(section, chunk_type))

    def _emit_items(self, section: _Section, items: list[Item]) -> None:
        for group in group_items(items, self.max_chars, self.min_item_chars):
            first, last = group[0].index, group[-1].index
            if len(group) == 1 and len(group[0].text) > ITEM_SPLIT_CHARS:
                parts = split_into_parts(group[0].blocks, SPLIT_PART_CHARS)
            else:
                parts = [BLOCK_JOINER.join(item.text for item in group)]
            meta = self._meta(section, "items")
            meta.update(
                item_index=first,
                item_last=last,
                item_count=len(group),
                authors=list(dict.fromkeys(name for item in group for name in item.authors)),
                commits=list(dict.fromkeys(link for item in group for link in item.commits)),
            )
            suffix = f":i{first}" if first == last else f":i{first}-{last}"
            self._chunk(section, suffix, parts, meta)


def parse_release_page(
    html: str | bytes,
    url: str,
    *,
    names: Iterable[str] | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    min_item_chars: int = DEFAULT_MIN_ITEM_CHARS,
) -> Release:
    """Chunks for one release page, in document order.

    ``names`` is the list of parameter names used to fill ``settings``; it is
    handed to ``pgdiag.chunks.find_settings``, whose default is every name in
    ``pgdiag.chunks.load_guc_names()`` except the everyday words ``port`` and
    ``role``. Raises ``ReleaseNotesError``
    when the page is not a release page or contains no release-note item.
    """
    return _ReleasePageParser(html, url, names, max_chars, min_item_chars).parse()
