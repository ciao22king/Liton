"""A tiny OpenAI-compatible chat client.

Liton works with no network and no key.  When ``OPENAI_API_KEY`` (or
``LITON_LLM_API_KEY``) is present, this client lets the planner ask a model to
refine the rule-based recipe.  The model is only ever an advisor: its output is
validated and never trusted blindly.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .util import Log

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


class LLMUnavailable(RuntimeError):
    pass


class LLMClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL,
                 model: str = DEFAULT_MODEL, timeout: float = 60.0, log: Log | None = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.log = log or Log("warn")

    @classmethod
    def from_env(cls, log: Log | None = None) -> "LLMClient":
        key = os.environ.get("LITON_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise LLMUnavailable("no LITON_LLM_API_KEY / OPENAI_API_KEY set")
        base = os.environ.get("LITON_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL
        model = os.environ.get("LITON_LLM_MODEL") or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
        return cls(key, base, model, log=log)

    @staticmethod
    def available() -> bool:
        return bool(os.environ.get("LITON_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY"))

    def complete(self, prompt: str, system: str | None = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }).encode()

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            raise LLMUnavailable(f"HTTP {exc.code} from {self.base_url}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMUnavailable(f"network error contacting {self.base_url}: {exc.reason}") from exc

        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailable(f"unexpected response shape: {str(body)[:200]}") from exc
