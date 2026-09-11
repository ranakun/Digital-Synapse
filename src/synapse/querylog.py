"""Query execution logging and reports."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from synapse.config import resolve_vault


def append(vault: str | Path | None, record: dict) -> None:
    try:
        root = resolve_vault(vault)
        log_dir = root / ".synapse"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "query-log.jsonl"
        
        # Ensure ts is in the record if not already present
        if "ts" not in record:
            record["ts"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as exc:
        sys.stderr.write(f"Warning: Failed to write to query log: {exc}\n")
