"""The BM25 tokenizer: case-insensitive, keeps PostgreSQL identifiers whole.

``tokenize(text)`` lower-cases the text and cuts it into ``[a-z0-9_]+`` words
(a word may continue with ``.<digits>``, so ``16.3`` stays one token). Then:

- An identifier with an inner underscore (``max_parallel_workers_per_gather``,
  ``pg_stat_statements``) is emitted whole, unstemmed, **and** followed by its
  parts treated as ordinary words (``max``, ``parallel``, ``worker``, ``per``,
  ``gather``). The whole token matches exact parameter names with a high IDF;
  the parts let "parallel workers" find the parameter's chunk.
- Ordinary words are dropped when they are English stop words or a single
  character, and stemmed with the Snowball English stemmer (``sorts`` ->
  ``sort``, ``checkpoints`` -> ``checkpoint``). Words containing a digit or a
  dot (``4mb``, ``16.3``) are kept as they are.

The same function is used for documents and queries. The BM25 index is not
stored on disk; it is rebuilt with this function every time the index is
loaded, so a change here takes effect without ``pgdiag-index build``. Bump
``TOKENIZER_VERSION`` anyway: it is reported next to the search results.
"""

from __future__ import annotations

import re
from functools import lru_cache

import Stemmer

TOKENIZER_VERSION = "1"
TOKENIZER_DESCRIPTION = (
    "lower-case [a-z0-9_]+ words; identifiers with '_' kept whole plus their parts; "
    "English stop words and 1-character words dropped; Snowball English stemming "
    "(not applied to whole identifiers or to words with digits)"
)

_WORD_RE = re.compile(r"[a-z0-9_]+(?:\.[0-9]+)*")

#: Common English function words. Configuration words such as "off" are kept.
STOPWORDS: frozenset[str] = frozenset(
    """
    a about above after again against all also am an and any are as at be because been
    before being below between both but by can could did do does doing down during each
    few for from further had has have having he her here hers herself him himself his how
    i if in into is it its itself just may me might more most must my myself no nor not of
    on once only or other our ours ourselves out over own same shall she should so some such
    than that the their theirs them themselves then there these they this those through to
    too under until up very via was we were what when where which while who whom why will
    with would you your yours yourself yourselves
    """.split()
)

_stemmer = Stemmer.Stemmer("english")


@lru_cache(maxsize=200_000)
def _stem(word: str) -> str:
    return _stemmer.stemWord(word)


def _add_word(word: str, out: list[str]) -> None:
    if len(word) < 2 or word in STOPWORDS:
        return
    if any(ch.isdigit() for ch in word) or "." in word:
        out.append(word)
    else:
        out.append(_stem(word))


def tokenize(text: str) -> list[str]:
    """BM25 tokens of ``text`` in order (duplicates kept)."""
    tokens: list[str] = []
    for raw in _WORD_RE.findall(text.lower()):
        word = raw.strip("_")
        if not word:
            continue
        if "_" in word:
            tokens.append(word)
            for part in word.split("_"):
                _add_word(part, tokens)
        else:
            _add_word(word, tokens)
    return tokens
