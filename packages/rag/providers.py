"""OpenAI-compatible provider adapters and dynamic model discovery.

No credential is read from process environment in this module.  Callers pass
the secret obtained from their credential provider.  Likewise, model IDs are
selected from ``/models`` using injected preferences instead of being embedded
in RAG business logic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .models import ProviderResponse


class ProviderUnavailable(RuntimeError):
    pass


class ProviderDisabled(ProviderUnavailable):
    pass


class ProviderCallError(RuntimeError):
    """A non-retryable provider failure."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RetryableProviderError(ProviderCallError):
    """Only timeout, HTTP 429 and HTTP 5xx failures use this type."""


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    content: str


class ChatProvider(Protocol):
    name: str
    model: str

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int,
    ) -> ProviderResponse: ...


class CompletionTransport(Protocol):
    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class ModelDiscoveryTransport(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]: ...


class HttpxCompletionTransport:
    def __init__(self, *, timeout: float = 60.0) -> None:
        self.client = httpx.Client(timeout=timeout)

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        response = self.client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, Mapping):
            raise TypeError("Provider returned a non-object JSON response")
        return value

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        response = self.client.get(url, headers=headers)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, Mapping):
            raise TypeError("Provider returned a non-object JSON response")
        return value

    def close(self) -> None:
        self.client.close()


def _classify_transport_error(exc: Exception, provider_name: str) -> ProviderCallError:
    if isinstance(exc, RetryableProviderError | ProviderCallError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return RetryableProviderError(f"{provider_name} timed out")
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        message = f"{provider_name} returned HTTP {status}"
        if status == 429 or status >= 500:
            return RetryableProviderError(message, status_code=status)
        return ProviderCallError(message, status_code=status)
    if isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError)):
        # The product contract intentionally limits automatic fallback to
        # timeout/429/5xx.  Other network failures are surfaced for review.
        return ProviderCallError(f"{provider_name} transport failed: {type(exc).__name__}")
    return ProviderCallError(f"{provider_name} request failed: {type(exc).__name__}")


def _citation_ids(text: str) -> tuple[str, ...]:
    """Extract stable ``[citation:<id>]`` markers from provider text."""

    prefix = "[citation:"
    values: list[str] = []
    offset = 0
    while True:
        start = text.find(prefix, offset)
        if start < 0:
            break
        end = text.find("]", start)
        if end < 0:
            break
        value = text[start + len(prefix) : end].strip()
        if value and value not in values:
            values.append(value)
        offset = end + 1
    return tuple(values)


class OpenAICompatibleProvider:
    name = "openai-compatible"

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str | None = None,
        transport: CompletionTransport | None = None,
        provider_name: str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.transport = transport or HttpxCompletionTransport()
        if provider_name:
            self.name = provider_name

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int,
    ) -> ProviderResponse:
        if not self.api_key:
            raise ProviderUnavailable(f"{self.name} API key is not configured")
        try:
            response = self.transport.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                payload={
                    "model": self.model,
                    "messages": [
                        {"role": message.role, "content": message.content}
                        for message in messages
                    ],
                    "max_tokens": max_output_tokens,
                    "temperature": 0,
                },
            )
        except Exception as exc:
            raise _classify_transport_error(exc, self.name) from exc
        choices = response.get("choices")
        if not isinstance(choices, Sequence) or not choices:
            raise ProviderUnavailable(f"{self.name} returned no completion")
        first = choices[0]
        if not isinstance(first, Mapping):
            raise ProviderUnavailable(f"{self.name} returned an invalid completion")
        message = first.get("message")
        if not isinstance(message, Mapping):
            raise ProviderUnavailable(f"{self.name} returned no message")
        text = str(message.get("content", "")).strip()
        usage = response.get("usage")
        usage_map = usage if isinstance(usage, Mapping) else {}
        return ProviderResponse(
            text=text,
            cited_ids=_citation_ids(text),
            input_tokens=int(usage_map.get("prompt_tokens", 0)),
            output_tokens=int(usage_map.get("completion_tokens", 0)),
            raw=dict(response),
        )


class DeepSeekAdapter(OpenAICompatibleProvider):
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str | None = None,
        transport: CompletionTransport | None = None,
    ) -> None:
        super().__init__(
            endpoint=endpoint,
            model=model,
            api_key=api_key,
            transport=transport,
            provider_name="deepseek",
        )


class MiniMaxAdapter(OpenAICompatibleProvider):
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str | None = None,
        transport: CompletionTransport | None = None,
    ) -> None:
        super().__init__(
            endpoint=endpoint,
            model=model,
            api_key=api_key,
            transport=transport,
            provider_name="minimax",
        )


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    model_id: str
    owned_by: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelPreference:
    """Configuration-driven primary and fallback selectors.

    Selectors are case-insensitive substrings matched against model IDs and
    optional provider metadata aliases.  Applications may source these values
    from settings; there are deliberately no DeepSeek model IDs here.
    """

    primary: tuple[str, ...]
    fallback: tuple[str, ...] = ()
    allow_first_available: bool = False

    @staticmethod
    def _matches(model: ModelDescriptor, selectors: Sequence[str]) -> bool:
        searchable = [model.model_id]
        aliases = model.metadata.get("aliases")
        if isinstance(aliases, Sequence) and not isinstance(aliases, (str, bytes)):
            searchable.extend(str(alias) for alias in aliases)
        normalized = tuple(value.casefold() for value in searchable)
        return any(
            selector.casefold() in value
            for selector in selectors
            for value in normalized
        )

    def select(self, models: Sequence[ModelDescriptor]) -> tuple[ModelDescriptor, ...]:
        selected: list[ModelDescriptor] = []
        for selectors in (self.primary, self.fallback):
            for model in models:
                if self._matches(model, selectors) and model not in selected:
                    selected.append(model)
        if not selected and self.allow_first_available and models:
            selected.append(models[0])
        return tuple(selected)


class ProviderSource(Protocol):
    def targets(self) -> tuple[ChatProvider, ...]: ...


@dataclass(slots=True)
class StaticProviderSource:
    providers: tuple[ChatProvider, ...]
    enabled: bool = True

    def targets(self) -> tuple[ChatProvider, ...]:
        return self.providers if self.enabled else ()


class DynamicOpenAIProviderSource:
    """Discover and materialize fixed-model OpenAI-compatible adapters."""

    def __init__(
        self,
        *,
        provider_name: str,
        chat_endpoint: str,
        models_endpoint: str,
        api_key: str | None,
        preference: ModelPreference,
        transport: CompletionTransport,
        discovery_transport: ModelDiscoveryTransport,
        enabled: bool = True,
    ) -> None:
        self.provider_name = provider_name
        self.chat_endpoint = chat_endpoint
        self.models_endpoint = models_endpoint
        self.api_key = api_key
        self.preference = preference
        self.transport = transport
        self.discovery_transport = discovery_transport
        self.enabled = enabled
        self._targets: tuple[ChatProvider, ...] | None = None

    def _discover(self) -> tuple[ModelDescriptor, ...]:
        if not self.api_key:
            raise ProviderUnavailable(f"{self.provider_name} API key is not configured")
        try:
            response = self.discovery_transport.get(
                self.models_endpoint,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except Exception as exc:
            raise _classify_transport_error(exc, self.provider_name) from exc
        raw_models = response.get("data")
        if not isinstance(raw_models, Sequence):
            raise ProviderUnavailable(f"{self.provider_name} returned no model list")
        models: list[ModelDescriptor] = []
        for raw in raw_models:
            if not isinstance(raw, Mapping) or not raw.get("id"):
                continue
            metadata = {
                str(key): value
                for key, value in raw.items()
                if key not in {"id", "owned_by"}
            }
            models.append(
                ModelDescriptor(
                    model_id=str(raw["id"]),
                    owned_by=(str(raw["owned_by"]) if raw.get("owned_by") else None),
                    metadata=metadata,
                )
            )
        return tuple(models)

    def targets(self) -> tuple[ChatProvider, ...]:
        if not self.enabled:
            return ()
        if self._targets is None:
            selected = self.preference.select(self._discover())
            if not selected:
                raise ProviderUnavailable(
                    f"{self.provider_name} has no model matching configured preferences"
                )
            self._targets = tuple(
                OpenAICompatibleProvider(
                    endpoint=self.chat_endpoint,
                    model=model.model_id,
                    api_key=self.api_key,
                    transport=self.transport,
                    provider_name=self.provider_name,
                )
                for model in selected
            )
        return self._targets


class DeepSeekProviderSource(DynamicOpenAIProviderSource):
    def __init__(
        self,
        *,
        chat_endpoint: str,
        models_endpoint: str,
        api_key: str | None,
        preference: ModelPreference,
        transport: CompletionTransport,
        discovery_transport: ModelDiscoveryTransport,
        enabled: bool = True,
    ) -> None:
        super().__init__(
            provider_name="deepseek",
            chat_endpoint=chat_endpoint,
            models_endpoint=models_endpoint,
            api_key=api_key,
            preference=preference,
            transport=transport,
            discovery_transport=discovery_transport,
            enabled=enabled,
        )


class MiniMaxProviderSource(DynamicOpenAIProviderSource):
    """MiniMax uses the same contract and is disabled until explicitly enabled."""

    def __init__(
        self,
        *,
        chat_endpoint: str,
        models_endpoint: str,
        api_key: str | None,
        preference: ModelPreference,
        transport: CompletionTransport,
        discovery_transport: ModelDiscoveryTransport,
        enabled: bool = False,
    ) -> None:
        super().__init__(
            provider_name="minimax",
            chat_endpoint=chat_endpoint,
            models_endpoint=models_endpoint,
            api_key=api_key,
            preference=preference,
            transport=transport,
            discovery_transport=discovery_transport,
            enabled=enabled,
        )


@dataclass(slots=True)
class ProviderChain:
    sources: tuple[ProviderSource, ...]
    discovery_errors: list[dict[str, Any]] = field(default_factory=list, init=False)

    def targets(self) -> tuple[ChatProvider, ...]:
        self.discovery_errors.clear()
        targets: list[ChatProvider] = []
        for source in self.sources:
            try:
                targets.extend(source.targets())
            except RetryableProviderError as exc:
                self.discovery_errors.append(
                    {
                        "outcome": "retryable_error",
                        "error": type(exc).__name__,
                        "status_code": exc.status_code,
                    }
                )
                continue
        if not targets and self.discovery_errors:
            raise RetryableProviderError("all provider model discovery attempts failed")
        return tuple(targets)
