"""Application ``.env`` loading.

Centralises how the backend discovers environment variables so every entry point
(the API and the import script) resolves the same ``backend/.env`` file before
reading settings from the environment.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

# This module lives at backend/src/common/env.py, so the backend root (which holds
# .env next to pyproject.toml) is three parents up.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]


def load_env(env_file: Path | None = None) -> None:
    """Load environment variables from a ``.env`` file.

    With no argument, loads ``backend/.env`` (next to ``pyproject.toml``) and then
    any ``.env`` found in the current working directory.
    """
    if env_file is not None:
        load_dotenv(env_file)
        return
    load_dotenv(_BACKEND_ROOT / ".env")
    load_dotenv()
