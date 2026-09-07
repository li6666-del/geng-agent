from __future__ import annotations

import json
import gzip
import hashlib
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4


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
    _request_audit_context: tuple[Path, str] | None = field(default=None, init=False, repr=False)
    _latest_request_audit: str | None = field(default=None, init=False, repr=False)

    @contextmanager
    def audit_requests(self, directory: Path, label: str):
        previous = self._request_audit_context
        self._request_audit_context = (directory, label)
        try:
            yield
        finally:
            self._request_audit_context = previous

    def _audit_request_body(self, body: bytes) -> None:
        self._latest_request_audit = None
        if self._request_audit_context is None:
            return
        directory, label = self._request_audit_context
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{label}_{uuid4().hex}_request.json.gz"
        # This is the exact HTTP body (including system, output format and
        # images), never the Authorization header or credentials.
        with path.open("xb") as stream:
            stream.write(gzip.compress(body, mtime=0))
        from .outputs import write_json
        write_json(path.with_suffix(".meta.json"), {
            "payload_path": str(path), "body_sha256": hashlib.sha256(body).hexdigest(),
            "body_bytes": len(body),
        })
        self._latest_request_audit = str(path)

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

        raw = self._post_chat_completion(payload)
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
        if self._latest_request_audit:
            entry["request_audit"] = self._latest_request_audit
        if isinstance(usage, dict):
            entry["prompt_tokens"] = usage.get("prompt_tokens")
            entry["completion_tokens"] = usage.get("completion_tokens")
            entry["total_tokens"] = usage.get("total_tokens")
        self.usage_log.append(entry)

    def _post_chat_completion(self, payload: dict[str, Any], *, allow_response_format_fallback: bool = True) -> str:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._audit_request_body(body)
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
                and any(token in detail.lower() for token in ("response_format", "json_schema", "schema"))
            ):
                fallback_payload = dict(payload)
                fallback_payload["response_format"] = {"type": "json_object"}
                # Only the output constraint changes. Preserve all messages
                # and images; a transport/vision failure is never text success.
                return self._post_chat_completion(fallback_payload, allow_response_format_fallback=False)
            raise RuntimeError(f"LLM request failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM request failed: {exc}") from exc

    def _chat_completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"
