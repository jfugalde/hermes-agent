"""Plugin-side tests for the Ollama Cloud web-search backend.

Covers:

- ``is_available()`` reflects key presence, with no network I/O.
- ``search()`` maps the vendor payload onto the legacy wire shape and clamps the limit.
- Key failover: the primary key's 429 (or transport error) falls through to the sibling key.
- Total failure returns ``{"success": False, ...}`` carrying the status detail, never raises.
- ``get_setup_schema()`` advertises the key requirement.

Per the sibling plugin suite's convention, the provider is imported for real (so ABC/registry
drift is caught) and only the two boundaries are faked: ``provider_env`` (key resolution) and
``httpx.post`` (the vendor call).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from plugins.web.ollama_cloud_websearch import provider as mod


class _Resp:
    def __init__(self, status_code: int, payload: Optional[Dict[str, Any]] = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Dict[str, Any]:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _patch_keys(monkeypatch: pytest.MonkeyPatch, keys: Dict[str, str]) -> None:
    monkeypatch.setattr(mod, "provider_env", lambda name: keys.get(name))


def _patch_post(monkeypatch: pytest.MonkeyPatch, calls: list, by_key: Dict[str, _Resp]) -> None:
    def fake_post(url: str, *, json: Dict[str, Any], headers: Dict[str, str], timeout: float) -> _Resp:
        key = headers["Authorization"].removeprefix("Bearer ")
        calls.append({"url": url, "body": json, "key": key, "timeout": timeout})
        return by_key[key]

    monkeypatch.setattr(mod.httpx, "post", fake_post)


# ---------------------------------------------------------------------------
# is_available()
# ---------------------------------------------------------------------------


def test_is_available_with_primary_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_keys(monkeypatch, {"OLLAMA_API_KEY": "k1"})
    assert mod.OllamaCloudWebSearchProvider().is_available() is True


def test_is_available_with_only_fallback_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_keys(monkeypatch, {"OLLAMA_API_KEY_FALLBACK": "k2"})
    assert mod.OllamaCloudWebSearchProvider().is_available() is True


def test_is_available_without_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_keys(monkeypatch, {})
    assert mod.OllamaCloudWebSearchProvider().is_available() is False


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------


def test_capabilities_and_identity() -> None:
    p = mod.OllamaCloudWebSearchProvider()
    assert p.name == "ollama-cloud-websearch"
    assert p.display_name == "Ollama Cloud Web Search"
    assert p.supports_search() is True
    assert p.supports_extract() is False


# ---------------------------------------------------------------------------
# search() happy path
# ---------------------------------------------------------------------------


def test_search_maps_results_and_clamps_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    payload = {
        "results": [
            {"title": "T1", "url": "https://a.example", "content": "c1"},
            {"title": "T2", "url": "https://b.example", "content": "c2"},
        ]
    }
    _patch_keys(monkeypatch, {"OLLAMA_API_KEY": "k1"})
    _patch_post(monkeypatch, calls, {"k1": _Resp(200, payload)})

    result = mod.OllamaCloudWebSearchProvider().search("q", 99)

    assert result["success"] is True
    web = result["data"]["web"]
    assert [h["url"] for h in web] == ["https://a.example", "https://b.example"]
    assert [h["position"] for h in web] == [1, 2]
    assert web[0]["title"] == "T1"
    assert calls[0]["body"]["max_results"] == mod.SEARCH_LIMIT_CAP
    assert calls[0]["url"] == "https://ollama.com/api/web_search"


def test_search_clamps_lower_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    _patch_keys(monkeypatch, {"OLLAMA_API_KEY": "k1"})
    _patch_post(monkeypatch, calls, {"k1": _Resp(200, {"results": []})})

    mod.OllamaCloudWebSearchProvider().search("q", 0)

    assert calls[0]["body"]["max_results"] == 1


# ---------------------------------------------------------------------------
# failover
# ---------------------------------------------------------------------------


def test_search_fails_over_from_capped_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 on the primary key must fall through to the healthy sibling."""
    calls: list = []
    _patch_keys(monkeypatch, {"OLLAMA_API_KEY": "capped", "OLLAMA_API_KEY_FALLBACK": "healthy"})
    _patch_post(
        monkeypatch,
        calls,
        {
            "capped": _Resp(429, {"error": {"message": "reached your Pro weekly limit"}}),
            "healthy": _Resp(200, {"results": [{"title": "T", "url": "https://ok.example", "content": "c"}]}),
        },
    )

    result = mod.OllamaCloudWebSearchProvider().search("q", 3)

    assert result["success"] is True
    assert result["data"]["web"][0]["url"] == "https://ok.example"
    assert [c["key"] for c in calls] == ["capped", "healthy"]


def test_search_survives_transport_error_on_first_key(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    _patch_keys(monkeypatch, {"OLLAMA_API_KEY": "k1", "OLLAMA_API_KEY_FALLBACK": "k2"})

    def flaky(url: str, *, json: Dict[str, Any], headers: Dict[str, str], timeout: float) -> _Resp:
        key = headers["Authorization"].removeprefix("Bearer ")
        calls.append(key)
        if key == "k1":
            raise OSError(113, "No route to host")
        return _Resp(200, {"results": [{"title": "T", "url": "https://ok.example", "content": "c"}]})

    monkeypatch.setattr(mod.httpx, "post", flaky)

    result = mod.OllamaCloudWebSearchProvider().search("q", 3)

    assert result["success"] is True
    assert calls == ["k1", "k2"]


def test_search_reports_failure_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    _patch_keys(monkeypatch, {"OLLAMA_API_KEY": "k1"})
    _patch_post(monkeypatch, calls, {"k1": _Resp(500, None, "boom")})

    result = mod.OllamaCloudWebSearchProvider().search("q", 3)

    assert result["success"] is False
    assert "500" in result["error"]


def test_search_without_any_key_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_keys(monkeypatch, {})

    result = mod.OllamaCloudWebSearchProvider().search("q", 3)

    assert result["success"] is False
    assert "no Ollama key" in result["error"]


# ---------------------------------------------------------------------------
# setup schema
# ---------------------------------------------------------------------------


def test_setup_schema_declares_key_env() -> None:
    schema = mod.OllamaCloudWebSearchProvider().get_setup_schema()
    assert [v["key"] for v in schema["env_vars"]] == ["OLLAMA_API_KEY"]
