"""Explicit visual teacher selection; credentials never enter recorded configuration."""

from __future__ import annotations

import os
import re
from pathlib import Path

PROVIDERS = ("claude", "glm")


def model_for(provider: str, model: str | None = None) -> str:
    if provider not in PROVIDERS:
        raise ValueError("unsupported visual teacher provider")
    return model or ("sonnet" if provider == "claude" else "glm-4.6v-flash")


def credential(name: str, path: Path | None = None) -> str:
    """Read one named value, with process environment taking precedence over dotenv."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("credential environment name is invalid")
    value = os.environ.get(name)
    if value is None and path is not None and Path(path).is_file():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            key, separator, candidate = line.strip().partition("=")
            if separator and key.strip() == name:
                value = candidate.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
    if not value or any(char.isspace() for char in value):
        raise ValueError(f"credential {name} is missing or invalid")
    return value


def make_vision_client(*, provider: str = "claude", model: str | None = None,
                       binary: str | None = None, base_url: str | None = None,
                       env_file: Path | None = None, key_env: str = "GLM_API_KEY",
                       effort: str | None = None):
    model = model_for(provider, model)
    if provider == "claude":
        from jev.play.teacher import ClaudeVisionClient

        if base_url is not None:
            raise ValueError("an API base URL applies only to the GLM provider")
        return ClaudeVisionClient(binary=binary, model=model, effort=effort)
    if effort is not None:
        raise ValueError("a reasoning effort applies only to the Claude provider")
    from jev.play.glm import ZAI_BASE_URL, GLMVisionClient

    return GLMVisionClient(api_key=credential(key_env, env_file), model=model,
                           base_url=base_url or ZAI_BASE_URL)
