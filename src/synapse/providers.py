"""LLM provider abstraction."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from synapse.config import load_config
from synapse.util import update_env_from_dotenv


@dataclass
class CompletionRequest:
    task: str
    prompt: str
    schema_name: str


@dataclass
class CompletionResult:
    data: dict[str, Any]
    model: str
    raw: str


class Generator(Protocol):
    def complete(self, request: CompletionRequest) -> CompletionResult: ...


class ProviderError(RuntimeError):
    pass


def _write_malformed_output(raw: str, schema_name: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        prefix=f"synapse-{schema_name}-malformed-",
        suffix=".txt",
    )
    with handle:
        handle.write(raw)
    return Path(handle.name)


class StaticGenerator:
    """Deterministic generator used by tests and demo smoke runs."""

    def __init__(self, data: dict[str, Any], model: str = "static-fixture") -> None:
        self.data = data
        self.model = model

    def complete(self, request: CompletionRequest) -> CompletionResult:
        return CompletionResult(data=self.data, model=self.model, raw=json.dumps(self.data))


class OpenAICompatibleGenerator:
    def __init__(self, vault: str | Path | None = None) -> None:
        self.vault = Path(vault or ".").resolve()
        update_env_from_dotenv(self.vault / ".env")
        self.config = load_config(self.vault)
        llm = self.config["llm"]
        self.base_url = llm["base_url"]
        self.model = llm["model"]
        if llm.get("zdr_required") and not llm.get("zdr_acknowledged"):
            raise ProviderError(
                "Remote calls require llm.zdr_acknowledged: true in .synapse/config.yaml"
            )
        if not os.environ.get("SYNAPSE_LLM_API_KEY"):
            raise ProviderError("SYNAPSE_LLM_API_KEY is not set")

    def complete(self, request: CompletionRequest) -> CompletionResult:
        try:
            from openai import OpenAI
        except Exception as exc:  # pragma: no cover - dependency-specific.
            raise ProviderError(
                "Install ingestion dependencies to use the remote generator"
            ) from exc

        client = OpenAI(api_key=os.environ["SYNAPSE_LLM_API_KEY"], base_url=self.base_url)
        messages = [
            {
                "role": "system",
                "content": "Return only valid JSON matching the requested Digital Synapse schema.",
            },
            {"role": "user", "content": request.prompt},
        ]
        last_raw = ""
        json_mode = True
        malformed_retry_used = False
        for attempt in range(3):
            kwargs = {
                "model": self.model,
                "messages": messages,
                "temperature": 0,
            }
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                response = client.chat.completions.create(**kwargs)
            except Exception:
                if json_mode:
                    json_mode = False
                    continue
                raise
            last_raw = response.choices[0].message.content or ""
            try:
                data = json.loads(last_raw)
                if not isinstance(data, dict):
                    raise ValueError("top-level JSON is not an object")
                return CompletionResult(data=data, model=self.model, raw=last_raw)
            except Exception as exc:
                if not malformed_retry_used and attempt < 2:
                    malformed_retry_used = True
                    messages.append({"role": "assistant", "content": last_raw})
                    messages.append(
                        {
                            "role": "user",
                            "content": "The previous output was invalid. Return JSON only.",
                        }
                    )
                    continue
                raw_path = _write_malformed_output(last_raw, request.schema_name)
                raise ProviderError(
                    f"Provider returned malformed JSON after retry; raw output: {raw_path}"
                ) from exc
        raw_path = _write_malformed_output(last_raw, request.schema_name)
        raise ProviderError(f"Provider returned malformed JSON after retry; raw output: {raw_path}")
