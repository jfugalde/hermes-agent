"""Same-provider /model must not walk the credential pool back to priority 0."""

from hermes_cli.model_switch import switch_model

_MOCK_VALIDATION = {
    "accepted": True,
    "persist": True,
    "recognized": True,
    "corrected_model": None,
    "message": "",
}


def test_same_provider_model_switch_keeps_rotated_key(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.models.validate_requested_model",
        lambda *a, **k: {**_MOCK_VALIDATION, "corrected_model": a[0]},
    )
    monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.model_switch.get_model_capabilities", lambda *a, **k: None
    )

    def _resolve(**kwargs):
        return {
            "api_key": "primary-key",
            "base_url": "https://ollama.com/v1",
            "api_mode": "chat_completions",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider", _resolve
    )

    result = switch_model(
        raw_input="deepseek-v4.1-flash",
        current_provider="ollama-cloud",
        current_model="deepseek-v4-pro:0813",
        current_base_url="https://ollama.com/v1",
        current_api_key="fallback-key",
        explicit_provider="ollama-cloud",
    )

    assert result.success is True
    assert result.api_key == "fallback-key"
    assert result.base_url == "https://ollama.com/v1"
    assert result.new_model == "deepseek-v4.1-flash"
