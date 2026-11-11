# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Model providers, the models the assistant offers for each role, and the prices its spend limits count with.

Shared rate card verified 4 November 2026 on the providers' public pricing pages.
Rates are dollars per million tokens, including cache and context-length adjustments.
Custom servers require an operator-configured price; unknown prices fail closed.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

ROLES = ("vision", "reasoning", "fast")
ROLE_LABELS = {
    "vision": "Looking at images",
    "reasoning": "Thinking and tools",
    "fast": "Quick answers",
}
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")

PROVIDERS = {
    "openai": {
        "label": "OpenAI",
        "protocol": "responses",
        "base_url": "https://api.openai.com/v1",
        "key_url": "https://platform.openai.com/api-keys",
    },
    "anthropic": {
        "label": "Anthropic",
        "protocol": "anthropic",
        "base_url": "https://api.anthropic.com",
        "key_url": "https://platform.claude.com/settings/keys",
    },
    "gemini": {
        "label": "Google Gemini",
        "protocol": "chat",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_url": "https://aistudio.google.com/app/apikey",
    },
    "openai-compatible": {
        "label": "Your own model server",
        "protocol": "chat",
        "base_url": "",  # the route says (an OpenAI-compatible gateway: LiteLLM, vLLM, Ollama)
        "key_url": "",
    },
}

MODELS: dict[str, dict[str, Any]] = {
    "gpt-6-astra": {
        "service": "openai",
        "label": "GPT-6 Astra",
        "input_rate": 10,
        "output_rate": 50,
        "roles": ["vision", "reasoning"],
    },
    "gpt-6.1-sol": {"service": "openai", "label": "GPT-6.1 Sol", "input_rate": 2, "output_rate": 10, "roles": ROLES},
    "gpt-6.1-terra": {"service": "openai", "label": "GPT-6.1 Terra", "input_rate": 1, "output_rate": 5, "roles": ROLES},
    "gpt-6-sol": {"service": "openai", "label": "GPT-6 Sol", "input_rate": 2, "output_rate": 10, "roles": ROLES},
    "gpt-6-luna": {
        "service": "openai",
        "label": "GPT-6 Luna",
        "input_rate": 0.1,
        "output_rate": 0.5,
        "roles": ["fast"],
    },
    "claude-opus-5-5": {
        "service": "anthropic",
        "label": "Claude Opus 5.5",
        "input_rate": 4,
        "output_rate": 20,
        "roles": ["vision", "reasoning"],
    },
    "claude-sonnet-5-5": {
        "service": "anthropic",
        "label": "Claude Sonnet 5.5",
        "input_rate": 2,
        "output_rate": 10,
        "roles": ROLES,
    },
    "claude-haiku-4-5": {
        "service": "anthropic",
        "label": "Claude Haiku 4.5",
        "input_rate": 1,
        "output_rate": 5,
        "roles": ["fast"],
    },
    "gemini-3.8-flash": {
        "service": "gemini",
        "label": "Gemini 3.8 Flash",
        "input_rate": 0.75,
        "output_rate": 3.75,
        "roles": ROLES,
    },
    "gemini-3.5-flash-lite": {
        "service": "gemini",
        "label": "Gemini 3.5 Flash-Lite",
        "input_rate": 0.3,
        "output_rate": 2.5,
        "roles": ["fast"],
    },
    "gemini-2.5-pro": {
        "service": "gemini",
        "label": "Gemini 2.5 Pro",
        "input_rate": 1.25,
        "output_rate": 10,
        "roles": ["vision", "reasoning"],
    },
}

# Provider/model-specific effort capabilities, shared with the Hub controls.
for _model, _entry in MODELS.items():
    if _model in {"gpt-6-astra", "gpt-6.1-sol", "claude-opus-5-5", "claude-sonnet-5-5"}:
        _entry["efforts"] = ["low", "medium", "high", "xhigh", "max"]
    elif _model.startswith("gemini-"):
        _entry["efforts"] = ["low", "medium", "high"]
    elif _model == "claude-haiku-4-5":
        _entry["efforts"] = ["none", "low", "medium", "high"]
    else:
        _entry["efforts"] = list(EFFORTS)


def efforts_for(model: str) -> tuple[str, ...]:
    return tuple(MODELS.get(model, {}).get("efforts", EFFORTS))


#: What a route gets by default per service: the best value for each role.
DEFAULTS = {
    "openai": {"vision": "gpt-6.1-sol", "reasoning": "gpt-6.1-sol", "fast": "gpt-6-luna"},
    "anthropic": {"vision": "claude-sonnet-5-5", "reasoning": "claude-sonnet-5-5", "fast": "claude-haiku-4-5"},
    "gemini": {"vision": "gemini-3.8-flash", "reasoning": "gemini-3.8-flash", "fast": "gemini-3.5-flash-lite"},
}

#: Cached input is charged at this share of the input rate (the providers' published ratio, rounded up).
CACHE_READ_SHARE = 0.1
# Standard rates verified 2026-11-03. Provider invoices remain authoritative.
CACHE_RATES = {"gpt-6.1-sol": 0.1, "gpt-6.1-terra": 0.05, "claude-sonnet-5-5": 0.1, "claude-opus-5-5": 0.2}


def _priced(model: str) -> dict[str, Any]:
    entry = MODELS.get(model)
    if model == "gpt-image-2.5-flare":
        return {"service": "openai", "input_rate": 8, "output_rate": 30}
    if entry is not None:
        return entry
    try:
        custom = json.loads(os.environ.get("TLC_AI_MODEL_PRICES", os.environ.get("TLC_ASSISTANT_MODEL_PRICES", "{}")))[
            model
        ]
        rates = {k: float(custom[k]) for k in ("input_rate", "output_rate")}
        if not all(math.isfinite(v) and v >= 0 for v in rates.values()):
            msg = "Invalid rates"
            raise ValueError(msg)
        return rates
    except (KeyError, ValueError, TypeError):
        msg = "This model has no verified price; configure TLC_ASSISTANT_MODEL_PRICES before use"
        raise ValueError(msg) from None


def models_for(service: str, role: str | None = None) -> list[dict[str, Any]]:
    """The priced models of ``service`` (that may serve ``role``), each with its id and label."""
    out = []
    for model_id, entry in MODELS.items():
        if entry["service"] != service or (role and role not in entry["roles"]):
            continue
        out.append({
            "id": model_id,
            "label": entry["label"],
            "input_rate": entry["input_rate"],
            "output_rate": entry["output_rate"],
            "roles": list(entry["roles"]),
            "efforts": list(efforts_for(model_id)),
        })
    return out


PRICING_VERSION = 2


def pricing_snapshot(model: str) -> dict[str, Any]:
    """Freeze the verified rates at admission, including operator-configured rates."""
    entry = _priced(model)
    return {
        "input_rate": entry["input_rate"],
        "output_rate": entry["output_rate"],
        "service": entry.get("service", "openai-compatible"),
        "cache_rate": CACHE_RATES.get(model, entry["input_rate"] * CACHE_READ_SHARE),
        **({"input_text_rate": 5, "input_image_rate": 8} if model == "gpt-image-2.5-flare" else {}),
    }


def cost_usd(model: str, usage: dict[str, Any], *, price: dict[str, Any] | None = None) -> float:
    """Standard provider-rate estimate from reported usage; unknown rates fail closed.

    Rates verified 2026-11-04 against provider pricing and prompt-caching docs.
    Cache-write lifetime components are disjoint; output includes reasoning tokens.
    """
    entry = pricing_snapshot(model) if price is None else price
    fields = ("input", "output", "cached", "cache_write", "cache_write_long")
    if any(type(usage.get(k, 0)) is not int or usage.get(k, 0) < 0 for k in fields):
        msg = "Invalid provider token usage"
        raise ValueError(msg)
    inputs, output = usage.get("input", 0), usage.get("output", 0)
    cached, writes, long_writes = (usage.get(k, 0) for k in fields[2:])
    if cached + writes > inputs or long_writes > writes:
        msg = "Provider cache counts exceed total input"
        raise ValueError(msg)
    if model == "gpt-image-2.5-flare":
        # If the provider omits modality counts, retain the conservative image rate.
        text = usage.get("input_text", 0)
        images = usage.get("input_image", inputs - text)
        if type(text) is not int or type(images) is not int or min(text, images) < 0 or text + images != inputs:
            msg = "Invalid image modality usage"
            raise ValueError(msg)
        return (
            float(
                text * entry.get("input_text_rate", 5)
                + images * entry.get("input_image_rate", 8)
                + output * entry["output_rate"]
            )
            / 1e6
        )
    long_context = (model == "gemini-2.5-pro" and inputs > 200000) or (
        model in {"gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna", "gpt-6-sol"} and inputs > 272000
    )
    long_rate = 2.0 if entry.get("service") == "anthropic" else 2.5
    amount = (
        (inputs - cached - writes) * entry["input_rate"]
        + (writes - long_writes) * entry["input_rate"] * 1.25
        + long_writes * entry["input_rate"] * long_rate
        + cached * entry["cache_rate"]
    )
    return (
        float(amount * (2 if long_context else 1) + output * entry["output_rate"] * (1.5 if long_context else 1)) / 1e6
    )


def ceiling_usd(model: str, input_tokens: int, max_output_tokens: int, *, price: dict[str, Any] | None = None) -> float:
    """Bound all inputs at the longest supported cache-write rate before dispatch."""
    price = pricing_snapshot(model) if price is None else price
    writes = (
        input_tokens if price.get("service") in {"anthropic", "openai"} and not model.startswith("gpt-image-") else 0
    )
    return cost_usd(
        model,
        {"input": input_tokens, "output": max_output_tokens, "cache_write": writes, "cache_write_long": writes},
        price=price,
    )


def public() -> dict[str, Any]:
    """What the settings page shows: providers, roles, efforts and the priced models per service."""
    return {
        "roles": [{"id": role, "label": ROLE_LABELS[role]} for role in ROLES],
        "efforts": list(EFFORTS),
        "providers": [
            {
                "id": pid,
                "label": p["label"],
                "key_url": p["key_url"],
                "models": models_for(pid),
                "defaults": DEFAULTS.get(pid, {}),
            }
            for pid, p in PROVIDERS.items()
        ],
    }
