from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import Any


def now_utc_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(data)
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_dumps_stable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def load_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file_handle:
        value = json.load(file_handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def save_json_dict(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json_dumps_stable(value)
    path.write_text(payload, encoding="utf-8")


def to_workdir_relative(path: Path, workdir: Path) -> str:
    path = path.resolve()
    try:
        return str(path.relative_to(workdir.resolve()))
    except ValueError:
        return str(path)


def from_workdir_relative(value: str, workdir: Path) -> Path:
    raw_path = Path(value)
    if raw_path.is_absolute():
        return raw_path
    return (workdir / raw_path).resolve()


def resolve_workdir(workdir_base: Path, edition: str, *, create: bool = True) -> Path:
    target = (workdir_base / edition).resolve()
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target
