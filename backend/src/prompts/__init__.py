"""Prompts for the knowledge-graph agent.

Each prompt lives in its own module; this package re-exports them so callers can
``from prompts import CYPHER_GENERATION_PROMPT, DEFAULT_EXAMPLES, RAG_TEMPLATE``.
"""

from __future__ import annotations

from prompts.answer_generation import AGENT_SYSTEM_PROMPT, RAG_TEMPLATE
from prompts.cypher_generation import CYPHER_GENERATION_PROMPT
from prompts.examples import DEFAULT_EXAMPLES

__all__ = ["AGENT_SYSTEM_PROMPT", "CYPHER_GENERATION_PROMPT", "DEFAULT_EXAMPLES", "RAG_TEMPLATE"]
