"""Vault configuration and initialization."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "vault_path": ".",
    "gate": {"auto_reindex_on_commit": True},
    "llm": {
        "base_url": "https://your-openai-compatible-gateway.example/v1",
        "model": "your-extraction-model",
        "zdr_required": True,
        "zdr_acknowledged": False,
        "tiering": {
            "enabled": False,
            "escalate_model": "your-escalation-model",
            "escalate_below": 0.6,
        },
    },
    "embeddings": {
        "enabled": False,
        "provider": "fastembed",
        "model": "BAAI/bge-small-en-v1.5",
    },
    "ingestion": {
        "inbox_dir": "inbox",
        "processed_dir": "inbox/processed",
        "auto_watch": False,
        "confidence_threshold": 0.6,
        "redaction": {"enabled": False, "patterns": []},
    },
    "index": {"db_path": ".synapse/index.db"},
}

_SECRET_SCAN_MARKER = "Digital Synapse secret scan"

_PRE_COMMIT_SECRET_SCAN = """#!/bin/sh
# Digital Synapse secret scan: block accidentally staged API keys.
PATTERN='(SYNAPSE_LLM_API_KEY|OPENROUTER_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY)[[:space:]]*=[[:space:]]*[^[:space:]#]+|sk-or-v1-[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]{20,}'
FILES=$(git diff --cached --name-only --diff-filter=ACM)
[ -z "$FILES" ] && exit 0
MATCHES=$(
  printf '%s\\n' "$FILES" |
  while IFS= read -r file; do
    case "$file" in
      .env.example|*/.env.example) continue ;;
    esac
    git show ":$file" 2>/dev/null |
      LC_ALL=C grep -nE -I "$PATTERN" |
      sed "s|^|$file:|"
  done
)
if [ -n "$MATCHES" ]; then
  echo "Digital Synapse secret scan blocked this commit." >&2
  echo "Remove staged API keys or secrets before committing:" >&2
  echo "$MATCHES" >&2
  exit 1
fi
exit 0
"""


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def resolve_vault(path: str | Path | None = None) -> Path:
    return Path(path or ".").expanduser().resolve()


def config_path(vault: Path) -> Path:
    return vault / ".synapse" / "config.yaml"


def load_config(vault: str | Path | None = None) -> dict[str, Any]:
    root = resolve_vault(vault)
    path = config_path(root)
    if not path.exists():
        config = dict(DEFAULT_CONFIG)
    else:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            loaded = {}
        config = deep_merge(DEFAULT_CONFIG, loaded)
    config["vault_path"] = str(root)
    return config


def save_config(vault: Path, config: dict[str, Any]) -> None:
    path = config_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def install_secret_pre_commit_hook(vault: Path) -> None:
    git_dir = vault / ".git"
    if not git_dir.exists():
        return
    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    if hook.exists():
        current = hook.read_text(encoding="utf-8", errors="replace")
        if _SECRET_SCAN_MARKER in current:
            return
        if current.strip():
            separator = "\n\n# ---- Digital Synapse extension ----\n"
            hook.write_text(
                current.rstrip() + separator + _PRE_COMMIT_SECRET_SCAN,
                encoding="utf-8",
            )
        else:
            hook.write_text(_PRE_COMMIT_SECRET_SCAN, encoding="utf-8")
    else:
        hook.write_text(_PRE_COMMIT_SECRET_SCAN, encoding="utf-8")
    try:
        hook.chmod(0o755)
    except OSError:
        pass


def init_vault(path: str | Path = ".", *, initialize_git: bool = True) -> Path:
    root = resolve_vault(path)
    for directory in [
        root / ".synapse",
        root / "entities" / "people",
        root / "entities" / "companies",
        root / "entities" / "projects",
        root / "entities" / "goals",
        root / "entities" / "finance",
        root / "ledgers",
        root / "inbox" / "processed",
    ]:
        directory.mkdir(parents=True, exist_ok=True)

    if not config_path(root).exists():
        config = dict(DEFAULT_CONFIG)
        config["vault_path"] = "."
        save_config(root, config)

    gitignore = root / ".gitignore"
    additions = {".synapse/index.db", ".synapse/*.db", ".synapse/*.db-*", ".env"}
    existing = (
        set(gitignore.read_text(encoding="utf-8").splitlines()) if gitignore.exists() else set()
    )
    merged = list(existing)
    for item in sorted(additions - existing):
        merged.append(item)
    gitignore.write_text(
        "\n".join(line for line in merged if line is not None).strip() + "\n", encoding="utf-8"
    )

    if initialize_git and not (root / ".git").exists():
        subprocess.run(["git", "init"], cwd=root, check=False, capture_output=True, text=True)
    if initialize_git:
        install_secret_pre_commit_hook(root)

    return root


def db_path(vault: str | Path | None = None) -> Path:
    root = resolve_vault(vault)
    cfg = load_config(root)
    return (root / cfg["index"]["db_path"]).resolve()
