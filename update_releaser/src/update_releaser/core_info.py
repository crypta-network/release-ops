from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from update_releaser.mapping import SUPPORTED_ARCHES, SUPPORTED_EXTENSIONS


@dataclass(slots=True, frozen=True)
class PackageDescriptor:
    chk: str | None = None
    store_url: str | None = None
    size: int | None = None

    def validate(self, package_key: str) -> None:
        has_chk = self.chk is not None
        has_store_url = self.store_url is not None
        if has_chk == has_store_url:
            raise ValueError(
                f"Package '{package_key}' must include exactly one of 'chk' or 'store_url'."
            )
        if self.size is not None and self.size < 0:
            raise ValueError(f"Package '{package_key}' has invalid negative size.")


@dataclass(slots=True, frozen=True)
class CoreInfoDescriptor:
    version: str
    release_page_url: str
    packages: dict[str, PackageDescriptor]
    changelog_chk: str | None = None
    fullchangelog_chk: str | None = None

    def validate(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("'version' must be a non-empty string.")
        if not isinstance(self.release_page_url, str) or not self.release_page_url:
            raise ValueError("'release_page_url' must be a non-empty string.")
        if not isinstance(self.packages, dict) or not self.packages:
            raise ValueError("'packages' must be a non-empty object.")
        for package_key, package in self.packages.items():
            validate_package_key(package_key)
            package.validate(package_key)
        if self.changelog_chk is not None and not isinstance(self.changelog_chk, str):
            raise ValueError("'changelog_chk' must be a string when provided.")
        if self.fullchangelog_chk is not None and not isinstance(
            self.fullchangelog_chk, str
        ):
            raise ValueError("'fullchangelog_chk' must be a string when provided.")


def validate_package_key(package_key: str) -> None:
    if "." not in package_key:
        raise ValueError(
            f"Invalid package key '{package_key}': expected <arch>.<ext> format."
        )
    arch, extension = package_key.split(".", 1)
    if arch not in SUPPORTED_ARCHES:
        raise ValueError(
            f"Invalid package key '{package_key}': arch must be one of {SUPPORTED_ARCHES}."
        )
    if extension not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Invalid package key '{package_key}': extension must be one of {SUPPORTED_EXTENSIONS}."
        )


def render_core_info_json(descriptor: CoreInfoDescriptor) -> str:
    descriptor.validate()

    payload: dict[str, Any] = {
        "version": descriptor.version,
        "release_page_url": descriptor.release_page_url,
    }
    if descriptor.changelog_chk:
        payload["changelog_chk"] = descriptor.changelog_chk
    if descriptor.fullchangelog_chk:
        payload["fullchangelog_chk"] = descriptor.fullchangelog_chk

    package_payload: dict[str, dict[str, Any]] = {}
    for package_key in sorted(descriptor.packages):
        package = descriptor.packages[package_key]
        package_data: dict[str, Any] = {}
        if package.chk is not None:
            package_data["chk"] = package.chk
        if package.store_url is not None:
            package_data["store_url"] = package.store_url
        if package.size is not None:
            package_data["size"] = package.size
        package_payload[package_key] = package_data

    payload["packages"] = package_payload
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def write_core_info_files(
    descriptor: CoreInfoDescriptor,
    *,
    workdir: Path,
    edition: str,
) -> Path:
    rendered = render_core_info_json(descriptor)
    workdir.mkdir(parents=True, exist_ok=True)

    core_info_path = workdir / "core-info.json"
    core_info_path.write_text(rendered, encoding="utf-8")

    audit_dir = workdir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    immutable_path = audit_dir / f"core-info.{edition}.json"
    if immutable_path.exists():
        existing = immutable_path.read_text(encoding="utf-8")
        if existing != rendered:
            raise RuntimeError(
                f"Immutable audit file already exists with different content: {immutable_path}"
            )
    else:
        immutable_path.write_text(rendered, encoding="utf-8")
    return core_info_path
