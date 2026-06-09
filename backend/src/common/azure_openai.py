"""Azure OpenAI configuration and client construction.

Builds a Microsoft Agent Framework :class:`OpenAIChatCompletionClient` (used by the
knowledge-graph agent for both planning and answer generation) from a single
:class:`AzureOpenAISettings`, handling both Azure AI Foundry / "v1" and classic Azure
OpenAI endpoints.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from agent_framework.openai import OpenAIChatCompletionClient

from common.logging_config import get_logger

DEFAULT_API_VERSION = "2024-10-21"

logger = get_logger(__name__)


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


def build_chat_client(settings: AzureOpenAISettings) -> OpenAIChatCompletionClient:
    """Create a Microsoft Agent Framework chat client for the agent.

    Azure AI Foundry / "v1" deployments expose an OpenAI-compatible surface reached via
    ``base_url``; classic Azure OpenAI resources use deployment routing via
    ``azure_endpoint`` + ``api_version``. Passing an explicit ``api_key`` (and
    ``azure_endpoint`` for classic) forces Azure routing regardless of any ambient
    ``OPENAI_API_KEY``.
    """
    endpoint = settings.endpoint.rstrip("/")
    if "/openai/v1" in endpoint:
        logger.debug("Building OpenAI-compatible (v1) chat client for deployment '%s'", settings.deployment)
        return OpenAIChatCompletionClient(model=settings.deployment, base_url=endpoint, api_key=settings.api_key)
    logger.debug(
        "Building classic Azure OpenAI chat client for deployment '%s' (api_version=%s)", settings.deployment, settings.api_version
    )
    return OpenAIChatCompletionClient(
        model=settings.deployment,
        azure_endpoint=endpoint,
        api_version=settings.api_version,
        api_key=settings.api_key,
    )
