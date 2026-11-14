"""Turn PostgreSQL 16 documentation pages (HTML from postgresql.org) into chunks.

Two kinds of chunk (``meta.chunk_type``):

``setting``
    On the server-configuration pages (``runtime-config-*.html``) every
    configuration parameter, i.e. every ``<dt id="GUC-...">`` term with its
    ``<dd>`` description, is exactly one chunk. It is never split, however long,
    and never merged with another parameter. ``meta.setting`` is the parameter
    name and the URL ends in the parameter's anchor.

    Special cases:

    * Several parameters documented in one entry (``debug_print_parse`` /
      ``debug_print_rewritten`` / ``debug_print_plan``, and the four
      ``log_*_stats`` parameters): each parameter gets its own chunk. The shared
      description is repeated and a "Documented together with" line names the
      others (also in ``meta.documented_with``), so every parameter can be found
      through ``meta.setting``. Only the first has a real anchor, which all of
      them use in their URL; the chunk ids are still distinct.
    * ``GUC-`` anchors nested inside a parameter's description (the cipher
      keywords under ``ssl_ciphers``) are values, not parameters, and stay part
      of the enclosing chunk.
    * ``max_replication_slots`` is documented twice, for sending servers and for
      subscribers, under two anchors. Both entries are kept as they are, so this
      one name has two setting chunks. None of the ten target settings is
      affected by any of these cases.

``section``
    Everything else is split by section, following the nested
    ``<div class="sect1|sect2|refsect1|...">`` elements of the DocBook output.
    A section's chunk holds only its own content; each subsection is a chunk
    of its own. Sections longer than ``max_chars`` are cut on block boundaries
    (paragraph, list item, table, code listing) into numbered parts.

Navigation headers and footers, tables of contents, index markers and the
"Submit correction" box are dropped. Tables become one line per row with
`` | `` between cells; code listings keep their line breaks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from pgdiag.chunks import Chunk, find_settings
from pgdiag.ingest.docs.crawl import PG_VERSION, page_name

SOURCE = "docs"
DEFAULT_MAX_CHARS = 1800  # bge-small-en reads at most 512 tokens
TITLE_SEPARATOR = " — "

CONFIG_PAGE_RE = re.compile(r"^runtime-config(-.+)?$")
RELEASE_PAGE_RE = re.compile(r"^release(-.+)?$")
#: Pages that produce no docs chunks. Release notes belong to T2.
SKIP_PAGES: frozenset[str] = frozenset({"index", "bookindex", "legalnotice"})
GUC_ID_PREFIX = "GUC-"
GENERATED_ID_RE = re.compile(r"^id-[0-9.]+$")

SECTION_CLASSES: frozenset[str] = frozenset(
    {
        "book",
        "part",
        "reference",
        "chapter",
        "appendix",
        "preface",
        "article",
        "sect1",
        "sect2",
        "sect3",
        "sect4",
        "sect5",
        "section",
        "simplesect",
        "refentry",
        "refsect1",
        "refsect2",
        "refsect3",
        "glossary",
        "glossdiv",
        "bibliography",
        "bibliodiv",
        "index",
        "indexdiv",
    }
)
#: Removed before any text is extracted.
BOILERPLATE_SELECTORS: tuple[str, ...] = (
    "div.navheader",
    "div.navfooter",
    "div.toc",
    "div.list-of-tables",
    "div.list-of-figures",
    "div.list-of-examples",
    "a.id_link",
    "a.indexterm",
    "script",
    "style",
    "#docComments",
)
BLOCK_TAGS: frozenset[str] = frozenset(
    {
        "p",
        "pre",
        "div",
        "table",
        "ul",
        "ol",
        "dl",
        "blockquote",
        "hr",
        "section",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "form",
        "address",
    }
)
HEADING_TAGS: frozenset[str] = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
ADMONITIONS: frozenset[str] = frozenset({"Note", "Tip", "Warning", "Caution", "Important"})


def is_config_page(page: str) -> bool:
    return bool(CONFIG_PAGE_RE.match(page))


def is_release_page(page: str) -> bool:
    return bool(RELEASE_PAGE_RE.match(page))


def is_skipped_page(page: str) -> bool:
    return page in SKIP_PAGES or is_release_page(page)


def clean(text: str) -> str:
    """Collapse all whitespace (including no-break spaces) to single spaces."""
    return " ".join(text.split())


# --------------------------------------------------------------------------
# Splitting long text
# --------------------------------------------------------------------------

_SENTENCE_END_RE = re.compile(r"(?<=[.!?:;])\s+")
BLOCK_JOINER = "\n\n"


def _units(block: str, max_chars: int, joiner: str = BLOCK_JOINER) -> list[tuple[str, str]]:
    """Cut a block into ``(joiner, text)`` units, each at most ``max_chars`` long.

    A block that fits is one unit. A longer one is cut on line breaks, else on
    sentence ends, else on spaces, else anywhere. ``joiner`` is what goes
    between a unit and the text before it when both end up in the same part.
    """
    if len(block) <= max_chars:
        return [(joiner, block)]
    for pattern, inner in ((r"\n", "\n"), (_SENTENCE_END_RE, " "), (r" +", " ")):
        pieces = [piece for piece in re.split(pattern, block) if piece]
        if len(pieces) > 1:
            units: list[tuple[str, str]] = []
            for index, piece in enumerate(pieces):
                units.extend(_units(piece, max_chars, inner if index else joiner))
            return units
    cuts = [block[i : i + max_chars] for i in range(0, len(block), max_chars)]
    return [(joiner if index == 0 else "", cut) for index, cut in enumerate(cuts)]


def split_into_parts(blocks: list[str], max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    """Join a section's blocks into parts of at most ``max_chars`` characters.

    Blocks (paragraphs, list items, tables, code listings) are separated by a
    blank line and kept whole when they fit; a part is closed when the next
    block does not fit. Only a block longer than ``max_chars`` is cut (see
    ``_units``). A short tail (under a tenth of ``max_chars``) is attached to
    the previous part, so a part may exceed ``max_chars`` by up to that much.
    """
    parts: list[str] = []
    current = ""
    last_joiner = BLOCK_JOINER  # what separates the newest part from the one before it
    for block in blocks:
        block = block.strip("\n").rstrip()
        if not block.strip():
            continue
        for joiner, text in _units(block, max_chars):
            if current and len(current) + len(joiner) + len(text) <= max_chars:
                current += joiner + text
            else:
                if current:
                    parts.append(current)
                current, last_joiner = text, joiner
    if current:
        parts.append(current)
    if len(parts) > 1 and len(parts[-1]) < max_chars // 10:
        tail = parts.pop()
        parts[-1] += last_joiner + tail
    return parts


# --------------------------------------------------------------------------
# HTML -> blocks of text
# --------------------------------------------------------------------------


@dataclass
class _Term:
    anchor: str | None  # the <dt id>, e.g. GUC-WORK-MEM
    name: str  # work_mem
    type: str | None  # integer
    text: str  # "work_mem (integer)"


@dataclass
class _Section:
    path: list[str]
    anchor: str  # unique on the page; goes into the chunk id
    fragment: str | None  # the element's id in the HTML, for the URL
    top: bool
    blocks: list[str] = field(default_factory=list)
    settings: list[tuple[list[_Term], list[str]]] = field(default_factory=list)
    subsections: list[Tag] = field(default_factory=list)


class _PageParser:
    def __init__(self, html: str | bytes, url: str, max_chars: int) -> None:
        self.url = url
        self.page = page_name(url) or "unknown"
        self.max_chars = max_chars
        self.config_page = is_config_page(self.page)
        self.soup = BeautifulSoup(html, "lxml")
        self.content = self.soup.find(id="docContent") or self.soup.body or self.soup
        self.page_title, self.parent_title = self._nav_titles()
        for selector in BOILERPLATE_SELECTORS:
            for element in self.soup.select(selector):
                element.decompose()
        self._anchors: set[str] = set()
        self._section_count = 0
        self._section: _Section | None = None
        self._in_setting = False
        self.chunks: list[Chunk] = []

    # ---- page-level -------------------------------------------------------

    def _nav_titles(self) -> tuple[str, str | None]:
        """Page title and parent ("Up") title from the navigation header."""
        title, parent = "", None
        header = self.content.find("div", class_="navheader")
        if header is not None:
            cells = header.find_all("th")
            if cells:
                title = clean(cells[0].get_text())
            if len(cells) > 1:
                parent = clean(cells[1].get_text()) or None
        if not title and self.soup.title is not None:
            title = clean(self.soup.title.get_text()).rsplit(": ", 1)[-1]
        return title or self.page, parent

    def parse(self) -> list[Chunk]:
        self._walk(self.content, [], top=True)
        return self.chunks

    # ---- sections ---------------------------------------------------------

    @staticmethod
    def _is_section(tag: Tag) -> bool:
        return tag.name == "div" and bool(SECTION_CLASSES.intersection(tag.get("class") or []))

    @staticmethod
    def _section_heading(node: Tag) -> Tag | None:
        """The section's own heading: inside its title page, or a bare ``<h2>`` child
        (reference pages: "Description", "Notes", ...)."""
        titlepage = node.find("div", class_="titlepage", recursive=False)
        heading = titlepage.find(HEADING_TAGS) if titlepage is not None else None
        if heading is None:
            heading = node.find(HEADING_TAGS, recursive=False)
        return heading

    def _new_anchor(self, node: Tag, title: str | None) -> tuple[str, str | None]:
        """``(anchor for the chunk id, fragment for the URL)`` of a section.

        The anchor is the element's id when it is a hand-written one
        (``PLANNER-STATS-EXTENDED``). Ids generated by the documentation build
        (``id-1.9.3.184.8``) still work as URL fragments but say nothing, so the
        chunk id uses the heading instead (``notes``), or ``sec-N`` without one.
        """
        self._section_count += 1
        element_id = node.get("id") or None
        if element_id and not GENERATED_ID_RE.match(element_id):
            anchor = element_id
        else:
            slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
            anchor = slug or f"sec-{self._section_count}"
        candidate, n = anchor, 1
        while candidate in self._anchors:
            n += 1
            candidate = f"{anchor}~{n}"
        self._anchors.add(candidate)
        return candidate, element_id

    def _walk(self, node: Tag, path: list[str], top: bool = False) -> None:
        is_wrapper = node is self.content
        heading: Tag | None = None
        if is_wrapper:
            # The content wrapper normally holds nothing but the page's outermost section.
            title, anchor, fragment = self.page_title, "top", None
            self._anchors.add(anchor)
        else:
            heading = self._section_heading(node)
            own_title = clean(heading.get_text()) if heading is not None else None
            # The outermost section carries the page title when it has no heading of its own.
            title = own_title or (self.page_title if top else None)
            anchor, fragment = self._new_anchor(node, own_title)
        section = _Section(
            path=path + [title] if title else list(path), anchor=anchor, fragment=fragment, top=top
        )
        outer, self._section = self._section, section
        section.blocks = self._blocks(node, skip_titlepage=True, skip=heading)
        self._section = outer

        self._emit_section(section)
        for terms, description in section.settings:
            self._emit_settings(section, terms, description)
        for child in section.subsections:
            self._walk(child, [] if is_wrapper else section.path, top=is_wrapper)

    # ---- blocks -----------------------------------------------------------

    def _blocks(
        self, node: Tag, *, skip_titlepage: bool = False, skip: Tag | None = None
    ) -> list[str]:
        """Flatten ``node`` into a list of text blocks (paragraph-sized units).

        ``skip_titlepage`` and ``skip`` leave out a section's own heading, which
        goes into the chunk title instead.
        """
        out: list[str] = []
        inline: list[str] = []
        label: str | None = None

        def emit(text: str) -> None:
            nonlocal label
            if not text.strip():
                return
            if label is not None:
                text = f"{label}: {text}" if label in ADMONITIONS else f"{label}\n{text}"
                label = None
            out.append(text)

        def flush_inline() -> None:
            text = clean("".join(inline))
            inline.clear()
            emit(text)

        def set_label(text: str) -> None:
            nonlocal label
            if label is not None:
                out.append(label)
            label = text or None

        for child in node.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                inline.append(str(child))
                continue
            if not isinstance(child, Tag) or child is skip:
                continue
            name = child.name
            if name not in BLOCK_TAGS:
                inline.append("\n" if name == "br" else child.get_text())
                continue
            flush_inline()
            classes = set(child.get("class") or [])
            if name == "div" and "titlepage" in classes:
                if not skip_titlepage:
                    set_label(clean(child.get_text()))
            elif self._section is not None and not self._in_setting and self._is_section(child):
                self._section.subsections.append(child)
            elif name in HEADING_TAGS or (name == "p" and "title" in classes):
                set_label(clean(child.get_text()))
            elif name == "p":
                emit(clean(child.get_text()))
            elif name == "pre":
                emit(child.get_text().strip("\n").rstrip())
            elif name == "table":
                emit(self._table(child))
            elif name in ("ul", "ol"):
                for number, item in enumerate(child.find_all("li", recursive=False), start=1):
                    bullet = f"{number}. " if name == "ol" else "- "
                    emit(bullet + "\n".join(self._blocks(item)))
            elif name == "dl":
                for entry in self._definition_list(child):
                    emit(entry)
            elif name == "hr":
                continue
            else:  # div, blockquote, ...: a transparent container
                for block in self._blocks(child):
                    emit(block)
        flush_inline()
        if label is not None:
            out.append(label)
        return out

    def _table(self, table: Tag) -> str:
        lines = []
        for row in table.find_all("tr"):
            if row.find_parent("table") is not table:
                continue  # belongs to a nested table, rendered with its cell
            cells = [
                " ".join(clean(block) for block in self._blocks(cell))
                for cell in row.find_all(["th", "td"], recursive=False)
            ]
            if any(cells):
                lines.append(" | ".join(cells))
        return "\n".join(lines)

    def _definition_list(self, dl: Tag) -> list[str]:
        """Render a ``<dl>``; parameter entries on configuration pages become setting chunks."""
        entries: list[str] = []
        terms: list[Tag] = []
        for child in dl.find_all(["dt", "dd"], recursive=False):
            if child.name == "dt":
                terms.append(child)
                continue
            guc_terms: list[_Term] = []
            if self.config_page and not self._in_setting and self._section is not None:
                if any((dt.get("id") or "").startswith(GUC_ID_PREFIX) for dt in terms):
                    guc_terms = self._guc_terms(terms)
            if guc_terms:
                self._in_setting = True
                description = self._blocks(child)
                self._in_setting = False
                self._section.settings.append((guc_terms, description))
            else:
                heading = " / ".join(filter(None, (clean(dt.get_text()) for dt in terms)))
                body = "\n".join(self._blocks(child))
                entries.append("\n".join(filter(None, (heading, body))))
            terms = []
        if terms:  # terms without a description
            entries.append(" / ".join(filter(None, (clean(dt.get_text()) for dt in terms))))
        return entries

    @staticmethod
    def _guc_terms(dts: list[Tag]) -> list[_Term]:
        terms: list[_Term] = []
        for dt in dts:
            spans = dt.find_all("span", class_="term") or [dt]
            for index, span in enumerate(spans):
                varname = span.find("code", class_="varname")
                if varname is None:
                    continue
                type_tag = span.find("code", class_="type")
                terms.append(
                    _Term(
                        anchor=dt.get("id") if index == 0 else None,
                        name=clean(varname.get_text()),
                        type=clean(type_tag.get_text()) if type_tag is not None else None,
                        text=clean(span.get_text()),
                    )
                )
        return terms

    # ---- chunks -----------------------------------------------------------

    def _base_meta(self, section: _Section) -> dict:
        meta = {
            "pg_version": PG_VERSION,
            "page": self.page,
            "page_title": self.page_title,
            "section_path": list(section.path),
        }
        if self.parent_title:
            meta["parent_title"] = self.parent_title
        return meta

    def _emit_section(self, section: _Section) -> None:
        parts = split_into_parts(section.blocks, self.max_chars)
        title = TITLE_SEPARATOR.join(section.path) or self.page_title
        url = self.url if section.top or not section.fragment else f"{self.url}#{section.fragment}"
        for number, text in enumerate(parts, start=1):
            many = len(parts) > 1
            self.chunks.append(
                Chunk(
                    chunk_id=f"{SOURCE}:{self.page}#{section.anchor}"
                    + (f":p{number}" if many else ""),
                    source=SOURCE,
                    url=url,
                    title=title + (f" (part {number}/{len(parts)})" if many else ""),
                    text=text,
                    settings=[],  # filled in by tag_settings()
                    meta={
                        **self._base_meta(section),
                        "chunk_type": "section",
                        "section_id": section.anchor,
                        "part": number,
                        "parts": len(parts),
                    },
                )
            )

    def _emit_settings(self, section: _Section, terms: list[_Term], description: list[str]) -> None:
        for term in terms:
            anchor = term.anchor or GUC_ID_PREFIX + term.name.upper().replace("_", "-")
            if anchor in self._anchors:
                raise ValueError(f"{self.url}: duplicate anchor {anchor} for setting {term.name}")
            self._anchors.add(anchor)
            lines = [term.text]
            others = [other.text for other in terms if other is not term]
            if others:
                lines.append("Documented together with: " + ", ".join(others))
            meta = {
                **self._base_meta(section),
                "chunk_type": "setting",
                "setting": term.name,
                "section_id": anchor,
                "part": 1,
                "parts": 1,
            }
            if term.type:
                meta["setting_type"] = term.type
            if others:
                meta["documented_with"] = [other.name for other in terms if other is not term]
            self.chunks.append(
                Chunk(
                    chunk_id=f"{SOURCE}:{self.page}#{anchor}",
                    source=SOURCE,
                    url=f"{self.url}#{term.anchor or terms[0].anchor or anchor}",
                    title=TITLE_SEPARATOR.join([*section.path, term.name]),
                    text="\n\n".join(["\n".join(lines), *description]),
                    settings=[term.name],
                    meta=meta,
                )
            )


def parse_page(html: str | bytes, url: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    """Chunks for one documentation page, in document order.

    ``settings`` is left empty on section chunks and holds only the chunk's own
    parameter on setting chunks; call ``tag_settings`` once the full list of
    parameter names is known. Pages in ``SKIP_PAGES`` and release-note pages
    yield nothing.
    """
    page = page_name(url)
    if page is None:
        raise ValueError(f"not a PostgreSQL {PG_VERSION} documentation page URL: {url}")
    if is_skipped_page(page):
        return []
    return _PageParser(html, url, max_chars).parse()


def setting_names(chunks: list[Chunk]) -> list[str]:
    """Names of all parameters that have a dedicated chunk, sorted."""
    return sorted({c.meta["setting"] for c in chunks if c.meta.get("chunk_type") == "setting"})


def tag_settings(chunks: list[Chunk], names: list[str] | tuple[str, ...]) -> None:
    """Fill ``settings`` on every chunk: the parameter names its text mentions."""
    for chunk in chunks:
        found = set(find_settings(chunk.text, names))
        if chunk.meta.get("chunk_type") == "setting":
            found.add(chunk.meta["setting"])
        chunk.settings = sorted(found)
