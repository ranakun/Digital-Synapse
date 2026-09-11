"""Fail legacy mutation routes before they create any v2 side effect."""

from pathlib import Path


def guard_legacy(vault, operation):
    from synapse.config import resolve_vault
    from synapse.revisions import require_legacy

    require_legacy(resolve_vault(vault), operation)


def guard_legacy_path(path: Path):
    from synapse.revisions import require_legacy

    # Defense in depth for internal helpers and direct frontmatter writers.
    # HEAD presence remains authoritative even if its bytes are corrupt.
    for parent in Path(path).absolute().parents:
        head = parent / "_synapse" / "HEAD"
        if head.exists() or head.is_symlink():
            require_legacy(parent, "write_frontmatter")
