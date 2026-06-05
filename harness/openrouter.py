"""Thin OpenRouter client — stdlib only, mirrors the wanderbench driver shape.

Tracks served providers (so we know which backend actually answered) and
exact $ cost (via OpenRouter's `usage.include = true`). Auto-retries on
transient errors with exponential backoff.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

OR_URL = "https://openrouter.ai/api/v1/chat/completions"

# Reasonable defaults for an OpenRouter benchmark client. Override per call.
_DEFAULT_MAX_TOKENS = 2048
_DEFAULT_TEMPERATURE = 0.6
_TIMEOUT_SEC = 180


class OpenRouterClient:
    """Stateful client: accumulates cost + provider set across calls."""

    def __init__(self, api_key: str, referer: str = "https://github.com/typhamswann/saguaro-bench",
                 title: str = "saguaro-bench"):
        self.api_key = api_key
        self.referer = referer
        self.title = title
        self.cost_usd = 0.0
        self.calls = 0
        self.served_providers: set[str] = set()
        self.last_usage: dict[str, Any] | None = None

    def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        provider: dict | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        temperature: float = _DEFAULT_TEMPERATURE,
        retries: int = 4,
    ) -> str:
        """Call /chat/completions, return the assistant message content as a string.
        Accumulates `self.cost_usd` and tracks served providers.
        """
        payload: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            # Ask OpenRouter for the exact $ amount billed for this call (gpt-style).
            "usage": {"include": True},
        }
        if provider:
            payload["provider"] = provider

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(OR_URL, data=body, headers={
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.referer,
            "X-Title": self.title,
        })

        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as r:
                    data = json.loads(r.read())
                # Cost + provider tracking
                if data.get("provider"):
                    self.served_providers.add(data["provider"])
                u = data.get("usage") or {}
                self.last_usage = u
                if u.get("cost") is not None:
                    try:
                        self.cost_usd += float(u["cost"])
                    except (TypeError, ValueError):
                        pass
                self.calls += 1
                # Extract text from the first choice — tolerate Anthropic-style
                # content-list returns by joining the text blocks.
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                content = msg.get("content", "")
                if isinstance(content, list):
                    parts = []
                    for p in content:
                        if isinstance(p, dict) and p.get("type") in ("text", None) and p.get("text"):
                            parts.append(p["text"])
                    content = "\n".join(parts)
                return content or ""
            except urllib.error.HTTPError as e:
                # 4xx (auth/billing/etc) is fatal; don't retry.
                last_err = e
                if 400 <= e.code < 500 and e.code not in (408, 429):
                    raise
                time.sleep(2 + 3 * attempt)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last_err = e
                time.sleep(2 + 3 * attempt)
        assert last_err is not None
        raise last_err
