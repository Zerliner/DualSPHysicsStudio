from __future__ import annotations

import dualsphysics_studio.ai.openai_provider as provider_module
from dualsphysics_studio.ai.models import ExplainResult
from dualsphysics_studio.ai.openai_provider import OpenAIProvider, SecretStore


def test_explain_schema_rejects_unknown_fields() -> None:
    payload = {
        "summary": "正常",
        "node_explanations": [],
        "risks": [],
        "suggested_edits": [],
    }
    result = ExplainResult.model_validate(payload)
    assert result.summary == "正常"


def test_missing_key_fails_before_network(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIProvider(api_key=None)
    monkeypatch.setattr(SecretStore, "get", lambda self: None)
    try:
        provider._resolve_key()
    except RuntimeError as exc:
        assert "API 密钥" in str(exc)
    else:
        raise AssertionError("missing API key must fail")


def test_secret_store_uses_fixed_windows_backend(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    class Backend:
        def get_password(self, service: str, username: str) -> str:
            calls.append(("get", service, username))
            return "saved-key"

        def set_password(self, service: str, username: str, value: str) -> None:
            calls.append(("set", service, username, value))

        def delete_password(self, service: str, username: str) -> None:
            calls.append(("delete", service, username))

    monkeypatch.setattr(provider_module, "_windows_credential_backend", Backend)
    store = SecretStore()

    assert store.get() == "saved-key"
    store.set("  new-key  ")
    store.delete()

    assert calls == [
        ("get", "DualSPHysics Studio", "openai-api-key"),
        ("set", "DualSPHysics Studio", "openai-api-key", "new-key"),
        ("delete", "DualSPHysics Studio", "openai-api-key"),
    ]
