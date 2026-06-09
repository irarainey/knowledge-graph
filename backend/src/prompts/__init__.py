"""Prompts for the knowledge-graph agent.

Each prompt lives in its own module; this package re-exports them so callers can
``from prompts import STRUCTURED_AGENT_SYSTEM_PROMPT``.
"""

from __future__ import annotations

from prompts.answer_generation import STRUCTURED_AGENT_SYSTEM_PROMPT

__all__ = ["STRUCTURED_AGENT_SYSTEM_PROMPT"]
