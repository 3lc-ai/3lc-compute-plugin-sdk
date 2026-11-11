# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Provider credential destinations, controlled by the deployment operator."""

import os
from urllib.parse import urlsplit

from .catalog import PROVIDERS


def endpoint(service: str, supplied: str = "") -> str:
    if service not in PROVIDERS:
        msg = "Unknown model service"
        raise ValueError(msg)
    canonical = PROVIDERS[service]["base_url"]
    supplied = supplied.strip().rstrip("/")
    if service != "openai-compatible":
        if supplied and supplied != canonical:
            msg = "Provider keys can only be sent to the provider's official endpoint"
            raise ValueError(msg)
        return canonical
    parts = urlsplit(supplied)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or any(c.isspace() for c in supplied)
    ):
        msg = "Model endpoint must be an http(s) URL without credentials, query or fragment"
        raise ValueError(msg)
    allowed = os.environ.get("TLC_AI_MODEL_ENDPOINTS", os.environ.get("TLC_ASSISTANT_MODEL_ENDPOINTS", "")).split()
    if supplied not in {a.rstrip("/") for a in allowed}:
        msg = "The deployment operator must allow this model endpoint in TLC_AI_MODEL_ENDPOINTS"
        raise ValueError(msg)
    return supplied
