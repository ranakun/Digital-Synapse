#!/usr/bin/env python3
"""
gen_synthetic_vault.py — Deterministic synthetic vault generator.

Produces a vault containing fake entities for performance benchmarking.
All output is reproducible (deterministic seed).  The generated entities
follow the canonical SDS Markdown shape; the vault can be opened by
``synapse reindex``, ``synapse stats``, and all query commands.

Usage::

    python scripts/gen_synthetic_vault.py \\
        --path /tmp/synth-vault \\
        --people 1000 \\
        --companies 100 \\
        --conversations 500

Exit codes: 0 on success, 1 on error.
"""

from __future__ import annotations

import argparse
import random
import shutil
import string
import sys
import textwrap
from pathlib import Path

# ── tiny alphabet for reproducible ULIDs ─────────────────────────────────────
_CHARS = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _fake_ulid(rng: random.Random) -> str:
    """Return a pseudo-ULID (26 Crockford-32 chars) using *rng*."""
    return "".join(rng.choice(_CHARS) for _ in range(26))


def _slug(name: str) -> str:
    allowed = string.ascii_lowercase + string.digits + "-"
    return "".join(c if c in allowed else "-" for c in name.lower()).strip("-")


# ── entity template renderers ─────────────────────────────────────────────────


def _person_md(uid: str, name: str, company_id: str | None, skill_names: list[str]) -> str:
    tags = ", ".join(f'"{s}"' for s in skill_names[:3])
    lines = [
        "---",
        f'id: "{uid}"',
        'type: person',
        f'name: "{name}"',
        'review_status: proposed',
        f'tags: [{tags}]',
    ]
    if company_id:
        lines += [
            'relations:',
            '  - type: works_at',
            f'    target: "{company_id}"',
        ]
    lines += ["---", "", "Synthetic person entity created by gen_synthetic_vault.py.", ""]
    return "\n".join(lines)


def _company_md(uid: str, name: str) -> str:
    lines = [
        "---",
        f'id: "{uid}"',
        'type: company',
        f'name: "{name}"',
        'review_status: proposed',
        "---",
        "",
        "Synthetic company entity created by gen_synthetic_vault.py.",
        "",
    ]
    return "\n".join(lines)


def _conversation_md(uid: str, name: str, participants: list[str]) -> str:
    lines = [
        "---",
        f'id: "{uid}"',
        'type: conversation',
        f'name: "{name}"',
        'review_status: proposed',
        'relations:',
    ]
    for pid in participants:
        lines += [
            '  - type: has_interaction',
            f'    target: "{pid}"',
            '    direction: incoming',
        ]
    lines += ["---", "", "Synthetic conversation entity.", ""]
    return "\n".join(lines)




# ── generator ─────────────────────────────────────────────────────────────────

_FIRST_NAMES = [
    "Alice", "Bob", "Carol", "Dave", "Eve", "Frank", "Grace", "Hank",
    "Iris", "Jack", "Kate", "Liam", "Mia", "Noah", "Olivia", "Paul",
    "Quinn", "Rose", "Sam", "Tina", "Uma", "Vince", "Wendy", "Xander",
    "Yara", "Zoe",
]
_LAST_NAMES = [
    "Smith", "Jones", "Brown", "Wilson", "Taylor", "Davies", "Evans",
    "Thomas", "Roberts", "Johnson", "Walker", "Wright", "Thompson",
    "White", "Hughes", "Edwards", "Green", "Hall", "Lewis", "Harris",
]
_COMPANY_SUFFIXES = ["Inc", "Ltd", "Corp", "Co", "Group", "Labs", "Works", "Tech"]
_SKILL_NAMES = [
    "Python", "SQL", "TypeScript", "Rust", "Go", "Java", "C++",
    "Machine Learning", "Data Analysis", "Project Management",
    "Leadership", "Communication", "Product Design", "DevOps",
]


def generate(
    root: Path,
    *,
    n_people: int,
    n_companies: int,
    n_conversations: int,
    seed: int = 42,
) -> None:
    rng = random.Random(seed)

    root.mkdir(parents=True, exist_ok=True)

    # ── .synapse config ───────────────────────────────────────────────────────
    synapse_dir = root / ".synapse"
    synapse_dir.mkdir(exist_ok=True)
    (synapse_dir / "config.yaml").write_text(
        textwrap.dedent("""\
            vault_path: .
            owner_entity_id: me
            gate:
              auto_reindex_on_commit: true
            llm:
              zdr_required: true
              zdr_acknowledged: false
            index:
              db_path: .synapse/index.db
            ingestion:
              inbox_dir: inbox
        """),
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(".synapse/index.db\n", encoding="utf-8")

    # ── entity directories ────────────────────────────────────────────────────
    for sub in ["people", "companies", "conversations", "skills",
                "opportunities", "events", "projects", "goals",
                "finance", "insights"]:
        (root / "entities" / sub).mkdir(parents=True, exist_ok=True)

    # ── owner (me) ────────────────────────────────────────────────────────────
    (root / "entities" / "people" / "me.md").write_text(
        textwrap.dedent("""\
            ---
            id: me
            type: person
            name: "Synthetic Owner"
            review_status: verified
            ---

            Vault owner placeholder created by gen_synthetic_vault.py.
        """),
        encoding="utf-8",
    )

    # ── companies ─────────────────────────────────────────────────────────────
    company_ids: list[str] = []
    for i in range(n_companies):
        uid = _fake_ulid(rng)
        adj = rng.choice(_FIRST_NAMES)
        suf = rng.choice(_COMPANY_SUFFIXES)
        name = f"{adj} {suf} {i}"
        company_ids.append(uid)
        sl = _slug(f"{adj}-{suf}-{i}")
        (root / "entities" / "companies" / f"{sl}.md").write_text(
            _company_md(uid, name), encoding="utf-8"
        )

    # ── people ────────────────────────────────────────────────────────────────
    person_ids: list[str] = []
    for i in range(n_people):
        uid = _fake_ulid(rng)
        fn = rng.choice(_FIRST_NAMES)
        ln = rng.choice(_LAST_NAMES)
        name = f"{fn} {ln} {i}"
        co = rng.choice(company_ids) if company_ids else None
        skills = rng.sample(_SKILL_NAMES, k=min(3, len(_SKILL_NAMES)))
        person_ids.append(uid)
        sl = _slug(f"{fn}-{ln}-{i}")
        (root / "entities" / "people" / f"{sl}.md").write_text(
            _person_md(uid, name, co, skills), encoding="utf-8"
        )

    # ── conversations ─────────────────────────────────────────────────────────
    all_participants = ["me"] + person_ids
    for i in range(n_conversations):
        uid = _fake_ulid(rng)
        name = f"Conversation {i}"
        k = rng.randint(1, min(3, len(all_participants)))
        participants = rng.sample(all_participants, k=k)
        sl = _slug(f"conv-{i}")
        (root / "entities" / "conversations" / f"{sl}.md").write_text(
            _conversation_md(uid, name, participants), encoding="utf-8"
        )

    total = 1 + n_people + n_companies + n_conversations
    print(
        f"Generated synthetic vault at {root!s}:\n"
        f"  {n_people} people, {n_companies} companies, "
        f"{n_conversations} conversations, 1 owner = {total} entities"
    )


# ── CLI ───────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate a deterministic synthetic SDS vault for benchmarking.",
    )
    p.add_argument("--path", required=True, type=Path, help="Output vault root directory.")
    p.add_argument("--people", type=int, default=500, help="Number of person entities.")
    p.add_argument("--companies", type=int, default=50, help="Number of company entities.")
    p.add_argument("--conversations", type=int, default=200, help="Number of conversation entities.")
    p.add_argument("--seed", type=int, default=42, help="Random seed (default 42).")
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Delete and recreate the output directory if it already exists.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out = Path(args.path).resolve()

    if out.exists():
        if not args.overwrite:
            print(
                f"Error: {out} already exists. Use --overwrite to replace it.",
                file=sys.stderr,
            )
            return 1
        shutil.rmtree(out)

    try:
        generate(
            out,
            n_people=args.people,
            n_companies=args.companies,
            n_conversations=args.conversations,
            seed=args.seed,
        )
    except Exception as exc:  # pragma: no cover
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
