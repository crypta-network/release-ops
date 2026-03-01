from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from update_releaser.github import ReleaseAsset

SUPPORTED_ARCHES = ("amd64", "arm64")
TAR_GZ_EXTENSION = "tar.gz"
SUPPORTED_EXTENSIONS = (
    "deb",
    "rpm",
    "dmg",
    "exe",
    "msi",
    TAR_GZ_EXTENSION,
    "zip",
    "pkg",
    "flatpak",
    "snap",
)

_IGNORED_EXACT = {
    "sha256sums.txt",
    "cryptad.jar",
}
_ARCH_RE = re.compile(r"(?:^|[-_.])(amd64|arm64)(?:[-_.]|$)")


class AssetMappingError(RuntimeError):
    """Raised when release assets cannot be mapped safely."""


@dataclass(frozen=True, slots=True)
class MappedAsset:
    package_key: str
    asset: ReleaseAsset
    arch: str
    extension: str


def is_ignored_asset(filename: str) -> bool:
    lowered = filename.lower()
    if lowered in _IGNORED_EXACT:
        return True
    if lowered.endswith(".sig") or lowered.endswith(".sig.txt"):
        return True
    return False


def detect_extension(filename: str) -> str | None:
    lowered = filename.lower()
    tar_gz_suffix = f".{TAR_GZ_EXTENSION}"
    if lowered.endswith(tar_gz_suffix):
        return TAR_GZ_EXTENSION
    for extension in SUPPORTED_EXTENSIONS:
        if extension == TAR_GZ_EXTENSION:
            continue
        if lowered.endswith(f".{extension}"):
            return extension
    return None


def map_asset_filename(filename: str) -> tuple[str, str, str] | None:
    """
    Return (package_key, arch, extension) for package assets.

    Returns None for ignored/non-package assets.
    Raises ValueError for package-like assets that cannot be mapped.
    """
    if is_ignored_asset(filename):
        return None

    extension = detect_extension(filename)
    if extension is None:
        return None

    tar_gz_suffix = f".{TAR_GZ_EXTENSION}"
    stem = (
        filename[: -len(tar_gz_suffix)]
        if extension == TAR_GZ_EXTENSION
        else filename[: -len(f".{extension}")]
    )
    arch_match = _ARCH_RE.search(stem.lower())
    if not arch_match:
        raise ValueError(
            f"Asset '{filename}' looks like a package (. {extension}) but has no supported arch token (amd64 or arm64)."
        )

    arch = arch_match.group(1)
    return f"{arch}.{extension}", arch, extension


def map_release_assets(assets: Iterable[ReleaseAsset]) -> dict[str, MappedAsset]:
    mapped: dict[str, MappedAsset] = {}
    unmapped: list[str] = []
    duplicates: list[str] = []

    for asset in assets:
        try:
            mapped_result = map_asset_filename(asset.name)
        except ValueError as exc:
            unmapped.append(str(exc))
            continue
        if mapped_result is None:
            continue
        package_key, arch, extension = mapped_result
        if package_key in mapped:
            duplicates.append(
                f"Package key '{package_key}' matched both '{mapped[package_key].asset.name}' and '{asset.name}'."
            )
            continue
        mapped[package_key] = MappedAsset(
            package_key=package_key,
            asset=asset,
            arch=arch,
            extension=extension,
        )

    if unmapped or duplicates:
        issues = "\n".join([*unmapped, *duplicates])
        raise AssetMappingError(f"Release assets contain unmapped or conflicting package files:\n{issues}")
    if not mapped:
        raise AssetMappingError(
            "No package assets were detected in the release. Check release artifacts and naming conventions."
        )
    return mapped
