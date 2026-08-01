"""OpenAI-compatible shim over the Cursor Agent SDK (`cursor-sdk`).

Hermes treats Cursor as a chat-completions backend. Each request formats the
conversation as a prompt, runs a local Cursor agent (default model ``auto``),
and maps the assistant text (+ optional Hermes ``<tool_call>`` blocks) back
into the OpenAI-shaped objects Hermes expects.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

CURSOR_MARKER_BASE_URL = "cursor://agent"
_DEFAULT_MODEL = "auto"

# Curated picker catalog — Auto first (survives named-model usage lockouts).
CURSOR_CURATED_MODELS = (
    "auto",
    "default",
    "composer-2.5",
    "composer-2",
    "composer-1.5",
)


def list_cursor_model_ids(*, api_key: str | None = None) -> list[str]:
    """Return Cursor model ids for ``/model`` / ``hermes model`` pickers.

    Prefers a live ``Cursor.models.list()`` catalog when ``CURSOR_API_KEY``
    works, always keeping curated Auto/default/Composer ids at the front.
    """
    curated = list(CURSOR_CURATED_MODELS)
    key = (api_key or "").strip() or (os.environ.get("CURSOR_API_KEY") or "").strip()
    if not key:
        return curated
    try:
        from cursor_sdk import Cursor  # type: ignore

        live_models = Cursor.models.list(api_key=key) or []
    except Exception:
        return curated

    live_ids: list[str] = []
    for item in live_models:
        mid = str(getattr(item, "id", "") or "").strip()
        if mid:
            live_ids.append(mid)

    if not live_ids:
        return curated

    merged = list(curated)
    seen = {m.lower() for m in merged}
    for mid in live_ids:
        if mid.lower() not in seen:
            merged.append(mid)
            seen.add(mid.lower())
    return merged


def _estimate_usage(messages, tools, response_text, reasoning_text=""):
    # Inlined: copilot_acp_client dropped its shared helper after the cursor
    # provider branch diverged from main.
    import json

    try:
        from agent.model_metadata import (
            estimate_request_tokens_rough,
            estimate_tokens_rough,
        )

        prompt_tokens = int(
            estimate_request_tokens_rough(messages or [], tools=tools) or 0
        )
        completion_tokens = int(
            estimate_tokens_rough(response_text or "")
            + estimate_tokens_rough(reasoning_text or "")
        )
    except Exception:
        prompt_tokens = max(1, len(json.dumps(messages or [])) // 4)
        completion_tokens = max(
            0, (len(response_text or "") + len(reasoning_text or "")) // 4
        )
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
    )


def _extract_tool_calls_from_text(text: str):
    from agent.copilot_acp_client import _extract_tool_calls_from_text as _ext

    return _ext(text)


def _format_messages_as_prompt(
    messages: list[dict[str, Any]],
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> str:
    from agent.copilot_acp_client import _format_messages_as_prompt as _fmt

    return _fmt(messages, model=model, tools=tools)


def _import_cursor_sdk():
    try:
        from cursor_sdk import Agent, AgentOptions, LocalAgentOptions  # type: ignore
        return Agent, AgentOptions, LocalAgentOptions
    except ImportError as exc:
        raise RuntimeError(
            "Cursor provider requires the `cursor-sdk` package. "
            "Install with: pip install cursor-sdk"
        ) from exc


def _is_bridge_death_error(exc: Exception) -> bool:
    """Detect a dead/stale bridge from the cursor SDK.

    Broadened beyond the original "connection refused" pair — the bridge
    subprocess can also die mid-request (broken pipe / early EOF / stream
    closed) or exit outright, all of which are recoverable the same way
    (kill the cached client, relaunch, retry).
    """
    msg = str(exc).lower()
    if ("bridge request failed" in msg and "connection refused" in msg) or (
        "connecterror" in msg and "connection refused" in msg
    ):
        return True
    return any(
        marker in msg
        for marker in (
            "broken pipe",
            "process has exited",
            "process exited",
            "bridge process exited",
            "eof when reading a line",
            "stream closed",
            "connection reset",
            "bridge crashed",
        )
    )


def _is_key_exhaustion_error(exc: Exception) -> bool:
    """Detect quota/auth failures on the current key that a second key might clear.

    Deliberately broader than bridge-death: covers cloud-side rejections
    (quota exhausted, rate limited, unauthorized) plus the generic
    "model provider attempt timeout" the SDK returns when it swallows the
    real upstream error. Retrying the SAME key on these is pointless; only
    a different key (CURSOR_API_KEY_FALLBACK) can help.
    """
    msg = str(exc).lower()
    return any(
        x in msg
        for x in (
            "model provider attempt timeout",
            "model_provider_attempt_timeout",
            "attempt timeout",
            "provider timeout",
            "api timeout",
            "quota",
            "rate limit",
            "rate_limit",
            "unauthorized",
            "invalid api key",
            "invalid_api_key",
            "forbidden",
        )
    )


_FALLBACK_ENV_VARS = (
    "CURSOR_API_KEY_FALLBACK",
    "CURSOR_API_KEY_FALLBACK2",
    "CURSOR_API_KEY_FALLBACK3",
    "CURSOR_API_KEY_FALLBACK4",
)


def _collect_fallback_api_keys() -> list[str]:
    """Return configured Cursor fallback keys, in retry order.

    Supports an arbitrary chain of fallback accounts: ``CURSOR_API_KEY_FALLBACK``
    (original, kept for backwards compatibility), then ``CURSOR_API_KEY_FALLBACK2``,
    ``CURSOR_API_KEY_FALLBACK3``, etc. Unset/empty vars are skipped; duplicates
    (e.g. a fallback var accidentally set to the same value as another) are
    de-duped so we don't retry the same exhausted key twice.
    """
    keys: list[str] = []
    seen: set[str] = set()
    for env_var in _FALLBACK_ENV_VARS:
        value = os.getenv(env_var, "").strip()
        if value and value not in seen:
            keys.append(value)
            seen.add(value)
    return keys


def _reset_cursor_bridge() -> None:
    """Force the cursor_sdk to discard its cached dead bridge."""
    try:
        from cursor_sdk._client import (  # type: ignore
            close_default_client,
            _DEFAULT_BRIDGE,
            _DEFAULT_CLIENT,
        )
        # Only clear if it looks dead (no process or process exited)
        if _DEFAULT_BRIDGE is not None:
            proc = getattr(_DEFAULT_BRIDGE, "process", None)
            if proc is not None and proc.poll() is not None:
                close_default_client()
                return
        # Fallback: always clear if we got here — the error already tells us
        # the bridge is unusable, so stale-caching is worse than a relaunch.
        close_default_client()
    except Exception:
        pass


class _CursorChatCompletions:
    def __init__(self, client: "CursorAgentClient"):
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client._create_chat_completion(**kwargs)


class _CursorChatNamespace:
    def __init__(self, client: "CursorAgentClient"):
        self.completions = _CursorChatCompletions(client)


class CursorAgentClient:
    """Minimal OpenAI-client-compatible facade for Cursor Agent SDK."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        cwd: str | None = None,
        **_: Any,
    ):
        self.api_key = (api_key or os.getenv("CURSOR_API_KEY", "")).strip()
        self._fallback_api_keys = _collect_fallback_api_keys()
        # Kept for backwards compatibility with anything importing this attr.
        self._fallback_api_key = (
            self._fallback_api_keys[0] if self._fallback_api_keys else ""
        )
        self.base_url = base_url or CURSOR_MARKER_BASE_URL
        self._default_headers = dict(default_headers or {})
        self._cwd = str(Path(cwd or os.getcwd()).resolve())
        self.chat = _CursorChatNamespace(self)
        self.is_closed = False
        self._lock = threading.Lock()
        self._agent = None

    def close(self) -> None:
        self.is_closed = True
        agent = self._agent
        self._agent = None
        if agent is None:
            return
        close = getattr(agent, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _create_chat_completion(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        **_: Any,
    ) -> Any:
        if not self.api_key:
            raise RuntimeError(
                "CURSOR_API_KEY is not set. Add it to Hermes .env "
                "(op://Infrastructure/Cursor API Key/credential)."
            )

        prompt = _format_messages_as_prompt(
            messages or [],
            model=model,
            tools=tools,
        )
        model_id = (model or _DEFAULT_MODEL).strip() or _DEFAULT_MODEL

        if stream:
            return self._stream_completion(
                prompt=prompt,
                model_id=model_id,
                messages=messages,
                tools=tools,
            )

        response_text = self._run_prompt(prompt, model_id=model_id)
        return self._build_completion(
            model=model_id,
            messages=messages,
            tools=tools,
            response_text=response_text,
        )

    def _build_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]] | None,
        tools: list[dict[str, Any]] | None,
        response_text: str,
    ) -> SimpleNamespace:
        tool_calls, cleaned = _extract_tool_calls_from_text(response_text)
        usage = _estimate_usage(messages, tools, response_text)
        message = SimpleNamespace(
            content=cleaned,
            tool_calls=tool_calls,
            reasoning=None,
            reasoning_content=None,
            reasoning_details=None,
        )
        finish_reason = "tool_calls" if tool_calls else "stop"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
            usage=usage,
            model=model,
        )

    def _run_prompt(self, prompt: str, *, model_id: str) -> str:
        """Run a prompt, walking the full key chain (primary, then each
        configured fallback in order) on exhaustion/rejection errors (quota,
        rate limit, auth, or the generic 'model provider attempt timeout' the
        SDK surfaces for cloud-side failures). Bridge-death is handled inside
        ``_run_prompt_with_key`` with its own retry and does not consume a
        key retry.

        Every key attempt is independent — a later request always starts
        from the primary key again, so a transient exhaustion on one request
        (e.g. a momentary rate limit) never permanently pins subsequent
        requests to a fallback account.
        """
        keys = [self.api_key, *self._fallback_api_keys]
        last_exc: Exception | None = None
        for index, key in enumerate(keys):
            if not key:
                continue
            try:
                text = self._run_prompt_with_key(prompt, model_id=model_id, api_key=key)
                return text
            except Exception as exc:  # noqa: BLE001 - re-raised below if unrecoverable
                last_exc = exc
                is_last_key = index == len(keys) - 1
                if is_last_key or not _is_key_exhaustion_error(exc):
                    raise
                _reset_cursor_bridge()
                continue
        # Unreachable in practice (self.api_key is validated non-empty by the
        # caller), but keeps type-checkers happy and fails loudly if it ever is.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Cursor provider has no usable API key configured.")

    def _run_prompt_with_key(
        self,
        prompt: str,
        *,
        model_id: str,
        api_key: str,
        max_bridge_retries: int = 2,
    ) -> str:
        Agent, AgentOptions, LocalAgentOptions = _import_cursor_sdk()
        options = AgentOptions(
            api_key=api_key,
            model=model_id,
            local=LocalAgentOptions(cwd=self._cwd),
        )
        with self._lock:
            last_exc: Exception | None = None
            for attempt in range(max_bridge_retries + 1):
                try:
                    return self._invoke_agent(Agent, options, prompt)
                except Exception as exc:
                    if not _is_bridge_death_error(exc) or attempt == max_bridge_retries:
                        raise
                    last_exc = exc
                    _reset_cursor_bridge()
                    # Brief backoff so a just-killed bridge subprocess has a
                    # moment to fully exit before we ask cursor_sdk to spawn
                    # a new one — avoids hammering a still-dying process.
                    time.sleep(min(0.5 * (attempt + 1), 2.0))
            # Defensive: loop above always returns or raises.
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("Cursor bridge invocation failed with no error captured.")

    def _invoke_agent(self, Agent: Any, options: Any, prompt: str) -> str:
        # Prefer Agent.prompt one-shot when available; fall back to create+send.
        if hasattr(Agent, "prompt"):
            result = Agent.prompt(prompt, options)
            status = getattr(result, "status", None)
            text = (
                getattr(result, "result", None)
                or getattr(result, "text", None)
                or getattr(result, "output", "")
                or ""
            )
            if status == "error":
                raise RuntimeError(f"Cursor agent run failed: {text or status}")
            return str(text or "")

        agent_ctx = Agent.create(options)
        with agent_ctx as agent:
            run = agent.send(prompt)
            wait = getattr(run, "wait", None)
            if callable(wait):
                wait()
            text = getattr(run, "result", None) or ""
            if hasattr(run, "messages"):
                parts: list[str] = []
                for message in run.messages():
                    if getattr(message, "type", None) != "assistant":
                        continue
                    content = getattr(getattr(message, "message", None), "content", None)
                    if isinstance(content, list):
                        for block in content:
                            if getattr(block, "type", None) == "text":
                                parts.append(str(getattr(block, "text", "") or ""))
                    elif isinstance(content, str):
                        parts.append(content)
                if parts:
                    text = "".join(parts)
            return str(text or "")

    def _stream_completion(
        self,
        *,
        prompt: str,
        model_id: str,
        messages: list[dict[str, Any]] | None,
        tools: list[dict[str, Any]] | None,
    ) -> Iterator[SimpleNamespace]:
        text = self._run_prompt(prompt, model_id=model_id)
        completion = self._build_completion(
            model=model_id,
            messages=messages,
            tools=tools,
            response_text=text,
        )
        from agent.copilot_acp_client import _completion_to_stream_chunks

        yield from _completion_to_stream_chunks(completion)
