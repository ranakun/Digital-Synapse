"""Vault configuration and initialization."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from synapse.models import ENTITY_TYPE_FOLDERS
from synapse.util import utc_now, write_frontmatter

DEFAULT_CONFIG: dict[str, Any] = {
    "vault_path": ".",
    "gate": {"auto_reindex_on_commit": True},
    "workspace": {"timezone": "UTC"},
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
PATTERN='(SYNAPSE_LLM_API_KEY|OPENROUTER_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY)[[:space:]]*=[[:space:]]*[^[:space:]#]+|(^|[^A-Za-z0-9])sk-(or-v1-)?[A-Za-z0-9_-]{20,}'
FILES=$(git diff --cached --name-only --diff-filter=ACM)
[ -z "$FILES" ] && exit 0
MATCHES=$(
  printf '%s\\n' "$FILES" |
  while IFS= read -r file; do
    [ "$file" = ".env.example" ] && continue
    [ "${file##*/}" = ".env.example" ] && continue
    LC_ALL=C git grep --cached -n -I -E "$PATTERN" -- "$file" 2>/dev/null
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

_TEMPLATES: dict[str, str] = {
    "person": """---
id: replace-with-ulid
type: person
name: Person Name
aliases: []
review_status: proposed
tags: []
relations: [] # Relation specs. Optional direction: incoming|outgoing
properties:
  first_name:
  last_name:
  current_company:
  current_role:
  linkedin_url:
  email:
  connected_on:
  relationship_strength:
  relationship_context:
  first_contacted_on:
  last_contacted_on:
  next_follow_up_on:
  career_relevance:
  source: []
  sensitivity:
---

# Person Name
""",
    "company": """---
id: replace-with-ulid
type: company
name: Company Name
aliases: []
review_status: proposed
tags: []
relations: [] # Relation specs. Optional direction: incoming|outgoing
properties:
  website:
  location:
  industry:
  priority:
  source: []
  sensitivity:
---

# Company Name
""",
    "opportunity": """---
id: replace-with-ulid
type: opportunity
name: Opportunity Name
aliases: []
review_status: proposed
tags: []
relations: [] # Relation specs. Optional direction: incoming|outgoing
properties:
  status: prospect
  company:
  role:
  location:
  compensation_range:
  source:
  priority:
  next_action:
  next_action_on:
  sensitivity:
---

# Opportunity Name
""",
    "conversation": """---
id: replace-with-ulid
type: conversation
name: YYYY-MM-DD Conversation
aliases: []
review_status: proposed
tags: []
relations: [] # Relation specs. Optional direction: incoming|outgoing
properties:
  date:
  channel:
  summary:
  commitments: []
  next_action:
  next_action_on:
  sensitivity:
---

# YYYY-MM-DD Conversation
""",
    "event": """---
id: replace-with-ulid
type: event
name: Event Name
aliases: []
review_status: proposed
tags: []
relations: [] # Relation specs. Optional direction: incoming|outgoing
properties:
  date:
  location:
  source:
  sensitivity:
---

# Event Name
""",
    "skill": """---
id: replace-with-ulid
type: skill
name: Skill Name
aliases: []
review_status: proposed
tags: []
relations: [] # Relation specs. Optional direction: incoming|outgoing
properties:
  proficiency:
  proof: []
  target_level:
  sensitivity:
---

# Skill Name
""",
    "project": """---
id: replace-with-ulid
type: project
name: Project Name
aliases: []
review_status: proposed
tags: []
relations: [] # Relation specs. Optional direction: incoming|outgoing
properties:
  role:
  outcome:
  technologies: []
  sensitivity:
---

# Project Name
""",
    "goal": """---
id: replace-with-ulid
type: goal
name: Goal Name
aliases: []
review_status: proposed
tags: []
relations: [] # Relation specs. Optional direction: incoming|outgoing
properties:
  timeframe:
  status:
  priority:
  sensitivity:
---

# Goal Name
""",
    "finance": """---
id: replace-with-ulid
type: finance
name: Finance Entity Name
aliases: []
review_status: proposed
tags: []
relations: [] # Relation specs. Optional direction: incoming|outgoing
properties:
  category:
  currency:
  sensitivity: confidential
---

# Finance Entity Name
""",
    "insight": """---
id: replace-with-ulid
type: insight
name: Insight Name
aliases: []
review_status: proposed
tags: []
relations: [] # Relation specs. Optional direction: incoming|outgoing
properties:
  topic:
  confidence:
  source:
  sensitivity:
---

# Insight Name
""",
}


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


def resolve_timezone(vault: str | Path | None = None, timezone: str | None = None) -> str:
    """Return an explicit or workspace-configured IANA timezone."""
    root = resolve_vault(vault)
    configured = load_config(root).get("workspace")
    value = timezone if timezone is not None else (
        configured.get("timezone", "UTC") if isinstance(configured, dict) else "UTC"
    )
    if not isinstance(value, str) or not value:
        raise ValueError("timezone must be a valid IANA timezone")
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {value}") from exc
    return value


def save_config(vault: Path, config: dict[str, Any]) -> None:
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault, "save_config")
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
            if (
                current.startswith("#!/bin/sh\n# Digital Synapse secret scan")
                and 'git show ":$file"' in current
            ):
                hook.write_text(_PRE_COMMIT_SECRET_SCAN, encoding="utf-8")
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


def _write_default_templates(root: Path) -> None:
    templates = root / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    for name, contents in _TEMPLATES.items():
        path = templates / f"{name}.md"
        if not path.exists():
            path.write_text(contents.strip() + "\n", encoding="utf-8")


def _write_owner_entity(root: Path, owner_name: str) -> None:
    path = root / "entities" / "people" / "me.md"
    if path.exists():
        return
    now = utc_now()
    write_frontmatter(
        path,
        {
            "id": "me",
            "type": "person",
            "name": owner_name.strip() or "Me",
            "aliases": [],
            "review_status": "verified",
            "tags": ["owner"],
            "relations": [],
            "properties": {"source": ["init"]},
            "created_at": now,
            "updated_at": now,
        },
        f"# {owner_name.strip() or 'Me'}\n\nOwner entity for this Digital Synapse vault.",
    )


def init_vault(
    path: str | Path = ".",
    *,
    initialize_git: bool = True,
    owner_name: str = "Me",
) -> Path:
    from synapse.legacy_guard import guard_legacy
    guard_legacy(path, "init_vault")
    root = resolve_vault(path)
    directories = [
        root / ".synapse",
        *[root / "entities" / folder for folder in ENTITY_TYPE_FOLDERS.values()],
        root / "ledgers",
        root / "reports",
        root / "templates",
        root / "inbox" / "processed",
    ]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)

    if not config_path(root).exists():
        config = dict(DEFAULT_CONFIG)
        config["vault_path"] = "."
        # v1 continues to create and address its stable owner record. New v2
        # workspaces use public_workspace.initialize_workspace instead.
        config["owner_entity_id"] = "me"
        save_config(root, config)
    else:
        config = load_config(root)
        if config.get("owner_entity_id") != "me":
            config["owner_entity_id"] = "me"
            save_config(root, config)

    _write_default_templates(root)
    _write_owner_entity(root, owner_name)

    gitignore = root / ".gitignore"
    additions = {".synapse/index.db", ".synapse/*.db", ".synapse/*.db-*", ".synapse/query-log.jsonl", ".env"}
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
