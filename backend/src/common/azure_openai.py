"""Azure OpenAI configuration and client construction.

Reusable across agents: builds both a neo4j-graphrag :class:`LLMInterface` (for
text-to-Cypher retrieval) and a Microsoft Agent Framework
:class:`OpenAIChatCompletionClient` (for answer generation) from a single
:class:`AzureOpenAISettings`. Both builders apply the same endpoint handling.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from agent_framework.openai import OpenAIChatCompletionClient
from neo4j_graphrag.llm import AzureOpenAILLM, OpenAILLM
from neo4j_graphrag.llm.base import LLMInterface

DEFAULT_API_VERSION = "2024-10-21"


@dataclass
class AzureOpenAISettings:
    endpoint: str
    api_key: str
    deployment: str
    api_version: str

    @classmethod
    def from_env(cls) -> AzureOpenAISettings:
        missing = [v for v in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT") if not os.environ.get(v)]
        if missing:
            raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")
        return cls(
            endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            deployment=os.environ["AZURE_OPENAI_DEPLOYMENT"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", DEFAULT_API_VERSION),
        )


def build_llm(settings: AzureOpenAISettings) -> LLMInterface:
    """Create a neo4j-graphrag LLM for the configured Azure OpenAI endpoint.

    Azure AI Foundry / "v1" deployments expose an OpenAI-compatible surface at
    ``<resource>/openai/v1`` and are reached with the plain OpenAI client via
    ``base_url``. Classic Azure OpenAI resources use deployment routing via
    ``azure_endpoint`` + ``api_version``.
    """
    endpoint = settings.endpoint.rstrip("/")
    if "/openai/v1" in endpoint:
        return OpenAILLM(model_name=settings.deployment, base_url=endpoint, api_key=settings.api_key)
    return AzureOpenAILLM(
        model_name=settings.deployment,
        azure_endpoint=endpoint,
        api_version=settings.api_version,
        api_key=settings.api_key,
    )


def build_chat_client(settings: AzureOpenAISettings) -> OpenAIChatCompletionClient:
    """Create a Microsoft Agent Framework chat client for answer generation.

    Mirrors :func:`build_llm`'s endpoint handling: Azure AI Foundry / "v1"
    deployments expose an OpenAI-compatible surface reached via ``base_url``;
    classic Azure OpenAI resources use deployment routing via ``azure_endpoint``
    + ``api_version``. Passing an explicit ``api_key`` (and ``azure_endpoint`` for
    classic) forces Azure routing regardless of any ambient ``OPENAI_API_KEY``.
    """
    endpoint = settings.endpoint.rstrip("/")
    if "/openai/v1" in endpoint:
        return OpenAIChatCompletionClient(model=settings.deployment, base_url=endpoint, api_key=settings.api_key)
    return OpenAIChatCompletionClient(
        model=settings.deployment,
        azure_endpoint=endpoint,
        api_version=settings.api_version,
        api_key=settings.api_key,
    )
