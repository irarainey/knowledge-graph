"""Deterministic relevance guardrail for the ``/ask`` pipeline.

A cheap, **no-LLM** check that rejects questions unrelated to the knowledge graph's
domain before any retrieval or generation runs. The relevance vocabulary is derived
from the live graph schema (node labels, relationship types and example values, which
name the actual things in the graph) plus a small curated set of domain keywords for
the natural-language phrasings users reach for that never appear verbatim in the
schema (e.g. "plane", "fly", "registration").

A question is considered in-scope if it shares at least one meaningful term with that
vocabulary. This is intentionally simple: it filters obvious off-topic questions
(greetings, general-knowledge, prompt-injection probes) without the cost or
non-determinism of an LLM judge.
"""

from __future__ import annotations

import re

# Natural-language domain terms users employ that may not appear verbatim in the
# schema. Keep this curated and small; the bulk of the vocabulary is schema-derived.
DOMAIN_KEYWORDS: frozenset[str] = frozenset(
    {
        "aircraft",
        "aeroplane",
        "airplane",
        "plane",
        "aviation",
        "aviate",
        "cessna",
        "skyhawk",
        "g-echo",
        "gecho",
        "fly",
        "flying",
        "flown",
        "flight",
        "flights",
        "pilot",
        "engine",
        "fuel",
        "oil",
        "propeller",
        "wing",
        "runway",
        "aerodrome",
        "airport",
        "takeoff",
        "landing",
        "taxi",
        "climb",
        "cruise",
        "descent",
        "approach",
        "avionics",
        "airframe",
        "powerplant",
        "maintenance",
        "airworthiness",
        "registration",
        "manufacturer",
        "model",
        "battery",
        "brake",
        "gear",
        "rudder",
        "aileron",
        "elevator",
        "flap",
        "magneto",
        "spark",
        "cylinder",
        "tyre",
        "tire",
        "wheel",
        "altimeter",
        "transponder",
        "radio",
        "document",
        "checklist",
        "specification",
        "speed",
        "hour",
        "hours",
    }
)

# Generic schema tokens that carry no domain signal on their own; excluded so a
# question like "what is your name?" can't pass on the ubiquitous "name" property.
GENERIC_TOKENS: frozenset[str] = frozenset(
    {
        "name",
        "names",
        "type",
        "types",
        "date",
        "dates",
        "id",
        "ids",
        "value",
        "values",
        "status",
        "number",
        "numbers",
        "description",
        "code",
        "label",
        "labels",
        "title",
        "key",
        "keys",
        "property",
        "properties",
        "node",
        "nodes",
        "relationship",
        "relationships",
        "start",
        "end",
        "example",
        "string",
        "int",
        "float",
        "bool",
        "list",
        "true",
        "false",
        "none",
        "null",
    }
)

# Common English words stripped from both the vocabulary and the question so overlap
# reflects domain terms, not filler.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "are",
        "was",
        "were",
        "with",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "when",
        "where",
        "why",
        "how",
        "does",
        "did",
        "has",
        "have",
        "had",
        "can",
        "could",
        "would",
        "should",
        "will",
        "shall",
        "may",
        "might",
        "must",
        "this",
        "that",
        "these",
        "those",
        "there",
        "here",
        "you",
        "your",
        "yours",
        "they",
        "them",
        "their",
        "from",
        "into",
        "about",
        "many",
        "much",
        "any",
        "all",
        "some",
        "each",
        "every",
        "list",
        "show",
        "tell",
        "give",
        "get",
        "find",
        "please",
        "between",
        "over",
        "under",
        "than",
        "then",
        "also",
        "not",
    }
)

_MIN_TOKEN_LENGTH = 3

# Split CamelCase / PascalCase / acronym-prefixed words into their parts, e.g.
# "PowerplantSystem" -> ["Powerplant", "System"], "GPSUnit" -> ["GPS", "Unit"].
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def _singularize(token: str) -> str:
    """Best-effort singular form so plurals match (``flights`` -> ``flight``)."""
    if len(token) > _MIN_TOKEN_LENGTH and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text: str) -> set[str]:
    """Lowercase, CamelCase-split and singularize ``text`` into meaningful terms."""
    tokens: set[str] = set()
    for word in _WORD_RE.findall(text):
        for part in _CAMEL_RE.findall(word):
            lowered = part.lower()
            if len(lowered) < _MIN_TOKEN_LENGTH or lowered.isdigit():
                continue
            if lowered in _STOPWORDS:
                continue
            tokens.add(_singularize(lowered))
    return tokens


def build_relevance_vocabulary(schema_text: str) -> frozenset[str]:
    """Build the in-scope term set from the graph schema plus curated domain keywords.

    Generic schema tokens (``name``, ``type``, ``date`` …) are excluded so they can't
    let off-topic questions through on an incidental match.
    """
    schema_tokens = tokenize(schema_text)
    keyword_tokens = {_singularize(token) for token in tokenize(" ".join(DOMAIN_KEYWORDS)) | DOMAIN_KEYWORDS}
    generic = {_singularize(token) for token in GENERIC_TOKENS}
    vocabulary = (schema_tokens | keyword_tokens) - generic
    return frozenset(vocabulary)


def is_relevant(question: str, vocabulary: frozenset[str]) -> bool:
    """Return ``True`` if the question shares at least one domain term with ``vocabulary``."""
    if not question or not question.strip():
        return False
    return bool(tokenize(question) & vocabulary)


# Streamed verbatim when a question is judged out of scope, so the API degrades to a
# polite refusal instead of attempting retrieval on an irrelevant question.
OFF_TOPIC_ANSWER = (
    "I can only answer questions about the aircraft knowledge graph — its systems, "
    "components, flights, aerodromes and maintenance. Please ask something about those."
)
