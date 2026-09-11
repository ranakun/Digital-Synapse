"""Strict codecs for the ``synapse-v2/1`` interchange contract.

This module owns shape validation and deterministic metadata hashing.  It does
not decide authority, evidence validity, identity resolution, or whether a
write is permitted.  Transport adapters may add request/correlation metadata
around the schema error payload; this codec deliberately does not invent IDs
because the error contract has no such field.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import math
import re
from collections.abc import Mapping
from datetime import date, datetime
from functools import cache, lru_cache
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_VERSION = "synapse-v2/1"
_SCHEMA_RESOURCE = "schemas/synapse-v2.schema.json"
_MAX_INTEROPERABLE_INTEGER = 9_007_199_254_740_991
_MIN_INTEROPERABLE_INTEGER = -_MAX_INTEROPERABLE_INTEGER
_SUPPORTED_KINDS = frozenset(
    {
        "source_version",
        "knowledge_record",
        "legacy_record",
        "request",
        "preparation_request",
        "result",
        "proposal",
        "receipt",
        "error",
    }
)
_ERROR_CODES = frozenset(
    {
        "invalid-request",
        "unsupported-operation",
        "ambiguous-identity",
        "source-unavailable",
        "invalid-span",
        "revision-expired",
        "revision-unavailable",
        "unsupported-history",
        "coverage-limited",
        "approval-required",
        "approval-revoked",
        "stale-selection",
        "external-edit-conflict",
        "idempotency-conflict",
        "precondition-expired",
        "recovery-required",
        "cancelled",
    }
)
_PUBLIC_ERROR_CODE_BY_INTERNAL_CODE = {
    "invalid-record": "invalid-request",
    "invalid-path": "invalid-request",
    "invalid-version": "invalid-request",
    "legacy-write-blocked": "unsupported-operation",
    "record-unavailable": "revision-unavailable",
    "object-integrity": "recovery-required",
    "manifest-integrity": "recovery-required",
    "invalid-source": "source-unavailable",
    "path-conflict": "invalid-request",
    "unsupported-platform": "unsupported-operation",
}
_WRITE_STATES = frozenset({"none", "committed", "unknown"})
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_TIME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

_FORMAT_CHECKER = FormatChecker()


@_FORMAT_CHECKER.checks("date")
def _is_date(value: Any) -> bool:
    if not isinstance(value, str) or not _DATE_PATTERN.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


@_FORMAT_CHECKER.checks("date-time")
def _is_date_time(value: Any) -> bool:
    if not isinstance(value, str) or not _DATE_TIME_PATTERN.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


class V2Error(ValueError):
    """A structured failure from the v2 contract boundary.

    ``details`` is retained for local diagnostics and is intentionally not
    copied into the wire payload: the schema's error payload has
    ``additionalProperties: false``.  A valid ``current_revision`` supplied
    in details is promoted to its schema-defined field.  Correlation IDs, when
    needed, belong to the caller's envelope/transport and are not fabricated
    here.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
        writes: str = "none",
    ) -> None:
        if not isinstance(code, str) or not code:
            raise TypeError("V2Error code must be a non-empty string")
        if not isinstance(message, str) or not message:
            raise TypeError("V2Error message must be a non-empty string")
        if not isinstance(retryable, bool):
            raise TypeError("V2Error retryable must be a boolean")
        if writes not in _WRITE_STATES:
            raise ValueError(f"V2Error writes must be one of {sorted(_WRITE_STATES)}")
        if details is not None and not isinstance(details, Mapping):
            raise TypeError("V2Error details must be a mapping or None")

        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details) if details is not None else None
        self.retryable = retryable
        self.writes = writes

    def to_dict(self) -> dict[str, Any]:
        """Return the schema-conforming ``error`` payload.

        The schema does not define a general details or correlation field, so
        only its required fields and a validated ``current_revision`` are
        serialized.  Internal codes are mapped to the closest public enum so
        this payload remains valid on the v2 wire.
        """

        payload: dict[str, Any] = {
            "code": self._public_code(),
            "message": self.message,
            "retryable": self.retryable,
            "writes": self.writes,
        }
        current_revision = self.details.get("current_revision") if self.details else None
        if _is_sha256_hex(current_revision):
            payload["current_revision"] = current_revision
        return payload

    def _public_code(self) -> str:
        if self.code in _ERROR_CODES:
            return self.code
        return _PUBLIC_ERROR_CODE_BY_INTERNAL_CODE.get(self.code, "invalid-request")


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    """Load the packaged schema without depending on the repository layout."""

    resource = importlib.resources.files("synapse").joinpath(_SCHEMA_RESOURCE)
    try:
        return json.loads(resource.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise V2Error("invalid-request", "The packaged Synapse v2 schema is unavailable.") from exc


@cache
def _envelope_validator(kind: str) -> Draft202012Validator:
    schema = _schema()
    branch = next(
        branch
        for branch in schema["oneOf"]
        if branch["properties"]["kind"]["const"] == kind
    )
    return Draft202012Validator(
        {
            "$schema": schema["$schema"],
            "$defs": schema["$defs"],
            **branch,
        },
        format_checker=_FORMAT_CHECKER,
    )


@cache
def _payload_validator(kind: str) -> Draft202012Validator:
    schema = _schema()
    return Draft202012Validator(
        {
            "$schema": schema["$schema"],
            "$defs": schema["$defs"],
            "$ref": f"#/$defs/{kind}",
        },
        format_checker=_FORMAT_CHECKER,
    )


def _is_sha256_hex(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _path_for_key(path: str, key: str) -> str:
    return f"{path}[{key!r}]"


def _validate_json_value(value: Any, *, path: str = "$", for_hash: bool = True) -> None:
    """Validate the strict JSON subset used by canonical metadata."""

    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if for_hash and not _MIN_INTEROPERABLE_INTEGER <= value <= _MAX_INTEROPERABLE_INTEGER:
            raise V2Error(
                "invalid-request",
                f"{path} contains an integer outside the interoperable range.",
                details={"path": path, "minimum": _MIN_INTEROPERABLE_INTEGER, "maximum": _MAX_INTEROPERABLE_INTEGER},
            )
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            reason = "non-finite floating-point values are not allowed"
        else:
            reason = "floating-point values are not allowed"
        raise V2Error("invalid-request", f"{path} contains {reason}.", details={"path": path})
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise V2Error(
                "invalid-request",
                f"{path} contains an unpaired Unicode surrogate.",
                details={"path": path},
            ) from exc
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]", for_hash=for_hash)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise V2Error(
                    "invalid-request",
                    f"{path} has a non-string object key.",
                    details={"path": path, "key_type": type(key).__name__},
                )
            _validate_json_value(item, path=_path_for_key(path, key), for_hash=for_hash)
        return
    raise V2Error(
        "invalid-request",
        f"{path} contains unsupported JSON type {type(value).__name__}.",
        details={"path": path, "type": type(value).__name__},
    )


def canonical_json(value: Any) -> bytes:
    """Encode strict JSON metadata with the v2 canonical serialization rules."""

    _validate_json_value(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise V2Error("invalid-request", "Value cannot be canonically encoded as v2 JSON.") from exc
    if encoded.endswith(b"\n"):
        raise V2Error("invalid-request", "Canonical JSON must not end with a newline.")
    return encoded


def hash_bytes(data: bytes) -> str:
    """Return SHA-256 for the exact supplied bytes."""

    if not isinstance(data, bytes):
        raise V2Error("invalid-request", "hash_bytes requires exact bytes.")
    return hashlib.sha256(data).hexdigest()


def _schema_error(error: Any, *, root: str) -> V2Error:
    path = list(error.absolute_path)
    location = root
    for part in path:
        location = f"{location}[{part!r}]" if isinstance(part, str) else f"{location}[{part}]"
    message = f"{location}: {error.message}"
    details = {
        "path": location,
        "validator": error.validator,
    }
    return V2Error("invalid-request", message, details=details)


def _check_kind(kind: Any) -> str:
    if not isinstance(kind, str) or kind not in _SUPPORTED_KINDS:
        raise V2Error(
            "unsupported-operation",
            f"Unknown Synapse v2 contract kind: {kind!r}.",
            details={"kind": kind},
        )
    return kind


def payload_schema(kind: str) -> dict[str, Any]:
    """Return a self-contained contract for an agent constructing a payload."""
    import copy
    _check_kind(kind)
    schema = _schema()
    needed, pending = {}, [kind]
    def references(value):
        if isinstance(value, dict):
            if "$ref" in value and value["$ref"].startswith("#/$defs/"):
                pending.append(value["$ref"].split("/")[-1])
            for item in value.values():
                references(item)
        elif isinstance(value, list):
            for item in value:
                references(item)
    while pending:
        name = pending.pop()
        if name not in needed:
            needed[name] = copy.deepcopy(schema["$defs"][name])
            references(needed[name])
    return {"$schema": schema["$schema"], "$ref": f"#/$defs/{kind}", "$defs": needed}


def validate_payload(kind: str, payload: Any) -> None:
    """Validate one known v2 payload against its Draft 2020-12 definition."""

    checked_kind = _check_kind(kind)
    _validate_json_value(payload, for_hash=False)
    errors = sorted(
        _payload_validator(checked_kind).iter_errors(payload),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        raise _schema_error(errors[0], root="payload")


def validate_envelope(envelope: Any) -> None:
    """Validate a complete v2 envelope, including date/time formats."""

    _validate_json_value(envelope, for_hash=False)
    if not isinstance(envelope, dict):
        raise V2Error("invalid-request", "A v2 envelope must be a JSON object.")
    if "kind" not in envelope:
        raise V2Error("invalid-request", "A v2 envelope requires a kind.", details={"path": "envelope.kind"})
    checked_kind = _check_kind(envelope["kind"])
    errors = sorted(
        _envelope_validator(checked_kind).iter_errors(envelope),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        raise _schema_error(errors[0], root="envelope")


def version_for(kind: str, payload: Mapping[str, Any]) -> str:
    """Hash the documented version projection for a source or proposal."""

    checked_kind = _check_kind(kind)
    if checked_kind not in {"source_version", "proposal"}:
        raise V2Error(
            "unsupported-operation",
            f"No version recipe is defined for contract kind {checked_kind!r}.",
            details={"kind": checked_kind},
        )
    if not isinstance(payload, Mapping):
        raise V2Error("invalid-request", "A versioned payload must be an object.")
    projection = dict(payload)
    projection.pop("version", None)
    if checked_kind == "proposal":
        projection.pop("semantic_review", None)
    return hash_bytes(canonical_json(projection))
