"""Small utility helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from random import SystemRandom
from typing import Any

import yaml

_RANDOM = SystemRandom()
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "entity"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    for index in range(2, 10000):
        candidate = parent / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find unique path for {path}")


def generate_ulid() -> str:
    """Generate a canonical 26-character ULID without external dependencies."""

    timestamp_ms = int(time.time() * 1000)
    random_bits = _RANDOM.getrandbits(80)
    value = (timestamp_ms << 80) | random_bits
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0b11111])
        value >>= 5
    return "".join(reversed(chars))


def write_frontmatter(path: Path, metadata: dict[str, Any], body: str) -> None:
    rendered = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{rendered}\n---\n\n{body.strip()}\n", encoding="utf-8")


def read_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8").lstrip("\ufeff")
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    body_start = end + len("\n---")
    if text[body_start : body_start + 2] == "\n\n":
        body_start += 2
    elif text[body_start : body_start + 1] == "\n":
        body_start += 1
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        data = {}
    return data, text[body_start:]


def update_env_from_dotenv(path: Path) -> None:
    """Load simple KEY=value pairs into process env without printing secrets."""

    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def vector_to_blob(values: list[float]) -> bytes:
    try:
        import numpy as np

        return np.asarray(values, dtype="float32").tobytes()
    except Exception:
        import array

        return array.array("f", values).tobytes()


def blob_to_vector(blob: bytes) -> list[float]:
    try:
        import numpy as np

        return np.frombuffer(blob, dtype="float32").astype("float32").tolist()
    except Exception:
        import array

        arr = array.array("f")
        arr.frombytes(blob)
        return list(arr)


def hash_embedding(text: str, dim: int = 64) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "big")
    values = []
    for index in range(dim):
        packed = hashlib.blake2b(f"{seed}:{index}".encode(), digest_size=4).digest()
        raw = int.from_bytes(packed, "big") / 0xFFFFFFFF
        values.append((raw * 2.0) - 1.0)
    norm = sum(v * v for v in values) ** 0.5 or 1.0
    return [v / norm for v in values]


def data_url_json(data: Any) -> str:
    encoded = base64.b64encode(json.dumps(data).encode("utf-8")).decode("ascii")
    return f"data:application/json;base64,{encoded}"
