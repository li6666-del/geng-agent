from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class LLMImage:
    label: str
    mime_type: str
    data_b64: str


class LLMClient(Protocol):
    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Return the assistant message content."""

    def complete_multimodal(
        self,
        prompt: str,
        *,
        images: list[LLMImage],
        system: str | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Return the assistant message content for text plus image inputs."""


@dataclass
class OpenAICompatibleClient:
    api_key: str
    base_url: str
    model: str
    temperature: float = 0.1
    timeout: float = 120.0
    thinking: str | None = None
    reasoning_effort: str | None = None
    # Cumulative per-call token usage as reported by the API (one entry per request).
    # Used by the pipeline to write run_cost.json; never affects request behaviour.
    usage_log: list[dict[str, Any]] = field(default_factory=list)

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if self.thinking:
            payload["thinking"] = {"type": self.thinking}
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if response_format:
            payload["response_format"] = response_format

        raw = self._post_chat_completion(payload)
        data = json.loads(raw)
        self._record_usage(data, kind="text")
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected LLM response shape: {raw}") from exc

    def complete_multimodal(
        self,
        prompt: str,
        *,
        images: list[LLMImage],
        system: str | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        if not images:
            raise ValueError("Multimodal completion requires at least one image.")

        content_parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in images:
            content_parts.append({"type": "text", "text": f"Image: {image.label}"})
            content_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{image.mime_type};base64,{image.data_b64}"},
                }
            )

        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content_parts})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if self.thinking:
            payload["thinking"] = {"type": self.thinking}
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if response_format:
            payload["response_format"] = response_format

        raw = self._post_chat_completion(payload, allow_response_format_fallback=False)
        data = json.loads(raw)
        self._record_usage(data, kind="multimodal")
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected LLM response shape: {raw}") from exc

    def _record_usage(self, data: Any, *, kind: str) -> None:
        """Append the API-reported token usage for one request. Missing/absent usage is
        recorded as an entry with null token counts so call counts stay accurate even when
        a provider omits the usage block."""
        usage = data.get("usage") if isinstance(data, dict) else None
        entry: dict[str, Any] = {"model": self.model, "kind": kind}
        if isinstance(usage, dict):
            entry["prompt_tokens"] = usage.get("prompt_tokens")
            entry["completion_tokens"] = usage.get("completion_tokens")
            entry["total_tokens"] = usage.get("total_tokens")
        self.usage_log.append(entry)

    def _post_chat_completion(self, payload: dict[str, Any], *, allow_response_format_fallback: bool = True) -> str:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._chat_completions_url(),
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            response_format = payload.get("response_format")
            if (
                allow_response_format_fallback
                and
                exc.code in {400, 422}
                and isinstance(response_format, dict)
                and response_format.get("type") == "json_schema"
            ):
                fallback_payload = dict(payload)
                fallback_payload["response_format"] = {"type": "json_object"}
                try:
                    return self._post_chat_completion(fallback_payload)
                except RuntimeError:
                    pass
            raise RuntimeError(f"LLM request failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM request failed: {exc}") from exc

    def _chat_completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"
