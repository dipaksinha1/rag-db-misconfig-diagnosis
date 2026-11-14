"""The chunk schema shared by every ingestion stage (T1, T2) and the index (T4).

A chunk is one retrievable piece of the knowledge base. Chunk files are JSON
Lines under ``data/chunks/`` (one object per line, UTF-8), one file per source:
``docs.jsonl`` and ``release_notes.jsonl``. The schema also allows a third
source, ``mailing_list`` (``mailing_list.jsonl``), but that stage (T3) was
skipped, so no such file or chunk exists; see ``PROGRESS.md``.

Fields
------
``chunk_id``  stable, unique across all sources, human readable, no whitespace.
              Starts with ``<source prefix>:`` (``docs:``, ``relnotes:``, ``ml:``),
              e.g. ``docs:runtime-config-resource#GUC-WORK-MEM``.
``source``    ``docs`` | ``release_notes`` | ``mailing_list``.
``url``       where the text came from (with ``#fragment`` when there is one).
``title``     short human-readable heading for the chunk.
``text``      the content, plain text.
``settings``  sorted, de-duplicated list of PostgreSQL configuration-parameter
              names mentioned in ``text`` (see ``find_settings``).
``meta``      source-specific fields, JSON-serialisable. For docs: ``chunk_type``
              (``setting`` | ``section``), ``setting`` (dedicated chunks only),
              ``page``, ``section_path``, ``part``, ``parts``, ``pg_version``.
              For release notes: ``chunk_type`` (``items`` | ``prose`` |
              ``overview``), ``version`` (``16.0``, ``16.3``), ``release_date``,
              ``page``, ``section``, ``section_path``, ``item_index`` and more
              (see ``pgdiag.ingest.release_notes.parse``).

Also here: ``find_settings`` (which parameter names a text mentions) and the
generated list of all PostgreSQL 16 parameter names (``load_guc_names``).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from pgdiag.config import Config, get_config
from pgdiag.settings import TARGET_SETTINGS

SOURCES: tuple[str, ...] = ("docs", "release_notes", "mailing_list")
#: chunk_id prefix for each source; ids look like ``<prefix>:<rest>``.
ID_PREFIXES: dict[str, str] = {"docs": "docs", "release_notes": "relnotes", "mailing_list": "ml"}

DOCS_CHUNKS_FILE = "docs.jsonl"
RELEASE_NOTES_CHUNKS_FILE = "release_notes.jsonl"
MAILING_LIST_CHUNKS_FILE = "mailing_list.jsonl"
GUC_NAMES_FILE = "guc_names.json"


class ChunkError(ValueError):
    """A chunk (or a chunk file) violates the schema."""


@dataclass
class Chunk:
    chunk_id: str
    source: str
    url: str
    title: str
    text: str
    settings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> Chunk:
        """Raise ``ChunkError`` if the chunk breaks the schema; return ``self`` otherwise."""

        def bad(problem: str) -> ChunkError:
            return ChunkError(f"chunk {self.chunk_id!r}: {problem}")

        for name in ("chunk_id", "source", "url", "title", "text"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise bad(f"{name} must be a non-empty string")
        if self.source not in SOURCES:
            raise bad(f"source must be one of {SOURCES}, got {self.source!r}")
        if re.search(r"\s", self.chunk_id):
            raise bad("chunk_id must not contain whitespace")
        if not self.chunk_id.startswith(ID_PREFIXES[self.source] + ":"):
            raise bad(f"chunk_id must start with {ID_PREFIXES[self.source] + ':'!r}")
        if not self.url.startswith("https://"):
            raise bad(f"url must be an https:// URL, got {self.url!r}")
        if not isinstance(self.settings, list) or not all(
            isinstance(s, str) for s in self.settings
        ):
            raise bad("settings must be a list of strings")
        if self.settings != sorted(set(self.settings)):
            raise bad("settings must be sorted and free of duplicates")
        if not isinstance(self.meta, dict):
            raise bad("meta must be a dict")
        try:
            json.dumps(self.meta)
        except (TypeError, ValueError) as exc:
            raise bad(f"meta is not JSON-serialisable: {exc}") from exc
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Chunk:
        fields = ("chunk_id", "source", "url", "title", "text", "settings", "meta")
        missing = [name for name in fields[:5] if name not in data]
        if missing:
            raise ChunkError(f"chunk {data.get('chunk_id', '?')!r}: missing fields {missing}")
        unknown = sorted(set(data) - set(fields))
        if unknown:
            raise ChunkError(f"chunk {data['chunk_id']!r}: unknown fields {unknown}")
        return cls(
            chunk_id=data["chunk_id"],
            source=data["source"],
            url=data["url"],
            title=data["title"],
            text=data["text"],
            settings=list(data.get("settings", [])),
            meta=dict(data.get("meta", {})),
        )


def validate_chunks(chunks: Iterable[Chunk]) -> list[Chunk]:
    """Validate every chunk and the uniqueness of ``chunk_id``; return them as a list."""
    result: list[Chunk] = []
    seen: set[str] = set()
    for chunk in chunks:
        chunk.validate()
        if chunk.chunk_id in seen:
            raise ChunkError(f"duplicate chunk_id {chunk.chunk_id!r}")
        seen.add(chunk.chunk_id)
        result.append(chunk)
    return result


def write_jsonl(path: Path, chunks: Iterable[Chunk]) -> int:
    """Validate and write chunks to ``path`` atomically; return how many were written."""
    validated = validate_chunks(chunks)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as handle:
        for chunk in validated:
            handle.write(json.dumps(chunk.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return len(validated)


def iter_jsonl(path: Path) -> Iterator[Chunk]:
    """Yield validated chunks from a chunk file."""
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                yield Chunk.from_dict(json.loads(line)).validate()
            except (json.JSONDecodeError, ChunkError) as exc:
                raise ChunkError(f"{path}:{number}: {exc}") from exc


def read_jsonl(path: Path) -> list[Chunk]:
    """Read a chunk file, validating every chunk and id uniqueness."""
    return validate_chunks(iter_jsonl(path))


# --------------------------------------------------------------------------
# Which configuration parameters does a text mention?
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Parameter names that are also everyday words. They are only reported when
#: passed explicitly as targets, never from the generated list of all names.
AMBIGUOUS_NAMES: frozenset[str] = frozenset({"port", "role"})


def save_guc_names(names: Iterable[str], cfg: Config | None = None, **extra: Any) -> Path:
    """Write the list of all parameter names to ``data/chunks/guc_names.json`` (T1 does this)."""
    cfg = cfg or get_config()
    path = cfg.chunks_dir / GUC_NAMES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**extra, "names": sorted(set(names))}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    load_guc_names.cache_clear()
    return path


@lru_cache(maxsize=4)
def load_guc_names(path: Path | None = None) -> tuple[str, ...]:
    """All known PostgreSQL parameter names, always including the ten targets.

    Read from ``data/chunks/guc_names.json`` (generated by ``pgdiag-ingest-docs``
    from the server-configuration pages). If that file does not exist yet, the
    result is just ``TARGET_SETTINGS``.
    """
    path = path or get_config().chunks_dir / GUC_NAMES_FILE
    names: set[str] = set(TARGET_SETTINGS)
    if path.is_file():
        names.update(json.loads(path.read_text(encoding="utf-8"))["names"])
    return tuple(sorted(names))


@lru_cache(maxsize=8)
def _matchers(names: tuple[str, ...]) -> tuple[dict[str, str], frozenset[str]]:
    """``(lower-cased name -> name, names matched case-sensitively)``."""
    targets = set(TARGET_SETTINGS)
    insensitive: dict[str, str] = {}
    sensitive: set[str] = set()
    for name in names:
        if name in targets or "_" in name:
            insensitive[name.lower()] = name
        elif name not in AMBIGUOUS_NAMES:
            sensitive.add(name)
    return insensitive, frozenset(sensitive)


def find_settings(text: str, names: Iterable[str] | None = None) -> list[str]:
    """Sorted parameter names mentioned in ``text``.

    ``names`` defaults to ``load_guc_names()`` (every PostgreSQL 16 parameter
    once T1 has run, the ten targets before that).

    Matching is on whole identifiers: the text is cut into
    ``[A-Za-z_][A-Za-z0-9_]*`` tokens and each token is compared with the
    names. So ``autovacuum`` is not found in ``autovacuum_naptime`` or
    ``log_autovacuum_min_duration``, and ``work_mem`` is not found in
    ``maintenance_work_mem``. The ten target settings and every name containing
    an underscore match case-insensitively (``Work_Mem``, ``Autovacuum``).
    Other single-word names (``jit``, ``ssl``, ``fsync``, ``DateStyle``) must
    match exactly, because in other spellings they usually mean the feature
    (``JIT``, ``SSL``) rather than the parameter. Names in ``AMBIGUOUS_NAMES``
    are skipped unless given explicitly in ``names``.
    """
    if names is None:
        insensitive, sensitive = _matchers(load_guc_names())
    else:
        explicit = tuple(names)
        insensitive, sensitive = _matchers(explicit)
        sensitive = sensitive | (AMBIGUOUS_NAMES & set(explicit))
    found: set[str] = set()
    for token in set(_TOKEN_RE.findall(text)):
        if token in sensitive:
            found.add(token)
        else:
            name = insensitive.get(token.lower())
            if name is not None:
                found.add(name)
    return sorted(found)
