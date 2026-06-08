from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse.providers import CompletionRequest, OpenAICompatibleGenerator, ProviderError


def test_openai_compatible_generator_retries_without_json_mode(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    config_dir = vault / ".synapse"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "base_url": "https://llm-gateway.example/v1",
                    "model": "gateway/free-json-model",
                    "zdr_required": True,
                    "zdr_acknowledged": True,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SYNAPSE_LLM_API_KEY", "sk-test-provider")

    calls: list[dict[str, object]] = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if "response_format" in kwargs:
                raise RuntimeError("model does not support JSON mode")
            message = types.SimpleNamespace(content='{"changeset": [], "ambiguities": []}')
            choice = types.SimpleNamespace(message=message)
            return types.SimpleNamespace(choices=[choice])

    class FakeOpenAI:
        def __init__(self, *, api_key: str, base_url: str) -> None:
            self.api_key = api_key
            self.base_url = base_url
            self.chat = types.SimpleNamespace(completions=FakeCompletions())

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    result = OpenAICompatibleGenerator(vault).complete(
        CompletionRequest(task="ingest", schema_name="changeset", prompt="{}")
    )

    assert result.data == {"changeset": [], "ambiguities": []}
    assert len(calls) == 2
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in calls[1]


def test_openai_compatible_generator_writes_malformed_output_to_temp_file(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    config_dir = vault / ".synapse"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "base_url": "https://llm-gateway.example/v1",
                    "model": "cheap-json-model",
                    "zdr_required": True,
                    "zdr_acknowledged": True,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SYNAPSE_LLM_API_KEY", "sk-test-provider")
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))

    class FakeCompletions:
        def create(self, **kwargs):
            message = types.SimpleNamespace(content="not json")
            choice = types.SimpleNamespace(message=message)
            return types.SimpleNamespace(choices=[choice])

    class FakeOpenAI:
        def __init__(self, *, api_key: str, base_url: str) -> None:
            self.chat = types.SimpleNamespace(completions=FakeCompletions())

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    with pytest.raises(ProviderError) as excinfo:
        OpenAICompatibleGenerator(vault).complete(
            CompletionRequest(task="ingest", schema_name="changeset", prompt="{}")
        )

    message = str(excinfo.value)
    assert "raw output" in message
    raw_path = Path(message.rsplit("raw output:", 1)[1].strip())
    assert raw_path.exists()
    assert raw_path.read_text(encoding="utf-8") == "not json"
