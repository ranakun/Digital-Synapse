"""Shared data structures for Digital Synapse."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ReviewStatus = Literal["proposed", "verified"]

ENTITY_TYPES = {"person", "company", "project", "goal", "finance"}
RELATION_TYPES = {
    "knows",
    "family_of",
    "reports_to",
    "manages",
    "introduced_by",
    "collaborates_with",
    "works_at",
    "former_employee_of",
    "founded",
    "advises",
    "invested_in",
    "leads",
    "contributes_to",
    "mentioned_in",
    "partner_of",
    "competitor_of",
    "subsidiary_of",
    "acquired",
    "has_goal",
    "supports",
    "blocks",
    "owns_account",
    "income_from",
    "obligation_to",
    "holds_asset",
    "related_to",
}


@dataclass(frozen=True)
class Issue:
    severity: Literal["error", "warning", "info"]
    message: str
    file_path: Path | None = None


@dataclass
class Relation:
    from_id: str
    to_id: str
    type: str
    weak: bool = False
    properties: dict[str, Any] = field(default_factory=dict)
    source_file: str | None = None
    review_status: ReviewStatus = "proposed"
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_id": self.from_id,
            "to_id": self.to_id,
            "type": self.type,
            "weak": self.weak,
            "properties": self.properties,
            "source_file": self.source_file,
            "review_status": self.review_status,
            "created_at": self.created_at,
        }


@dataclass
class Entity:
    id: str
    type: str
    name: str
    file_path: Path
    frontmatter: dict[str, Any]
    body: str
    content_hash: str
    review_status: ReviewStatus = "proposed"
    aliases: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None
    relation_specs: list[dict[str, Any]] = field(default_factory=list)
    weak_refs: list[str] = field(default_factory=list)

    @property
    def relations(self) -> list[dict[str, Any]]:
        return self.relation_specs

    @property
    def wikilinks(self) -> list[str]:
        return self.weak_refs

    @property
    def weak_links(self) -> list[str]:
        return self.weak_refs

    @property
    def body_links(self) -> list[str]:
        return self.weak_refs

    def search_text(self) -> str:
        return "\n".join(
            [
                self.name,
                " ".join(self.aliases),
                " ".join(self.tags),
                self.body,
            ]
        )

    def to_node(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "file_path": str(self.file_path),
            "review_status": self.review_status,
            "aliases": self.aliases,
            "tags": self.tags,
            "properties": self.properties,
        }


@dataclass
class ReindexResult:
    entities: int = 0
    relations: int = 0
    changed_files: int = 0
    issues: list[Issue] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)


@dataclass
class QueryResult:
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {"nodes": self.nodes, "edges": self.edges}
