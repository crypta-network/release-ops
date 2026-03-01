from __future__ import annotations

import json
import logging
from pathlib import Path
import re
from typing import Any

from update_releaser.core_info import validate_package_key
from update_releaser.fcp_client import FCPClient, FCPClientError
from update_releaser.state import now_utc_iso, save_json_dict

LOGGER = logging.getLogger(__name__)

_CHK_PREFIXES = ("CHK@", "freenet:CHK@")
_PACKAGE_KEY_RE = re.compile(r"^(amd64|arm64)\.(.+)$")


def verify_published_descriptor(
    fcp: FCPClient,
    *,
    descriptor_uri: str,
    workdir: Path,
    fallback_descriptor_uri: str | None = None,
    expected_version: str | None = None,
    expected_release_page_url: str | None = None,
    timeout_s: int = 60,
    deep: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "descriptor_uri": descriptor_uri,
        "checked_at": now_utc_iso(),
        "deep": deep,
        "schema_errors": [],
        "identity_errors": [],
        "descriptor_fetch_fallback_used": False,
        "descriptor_fetch_source": "requested",
        "chk_checks": [],
        "ok": False,
        "dry_run": dry_run,
    }

    if dry_run:
        report["ok"] = True
        _write_verify_report(workdir, report)
        return report

    resolved_descriptor_uri = descriptor_uri
    try:
        descriptor_bytes = fcp.get_bytes(descriptor_uri, timeout_s=timeout_s)
    except FCPClientError as primary_exc:
        can_fallback = fallback_descriptor_uri and fallback_descriptor_uri != descriptor_uri
        if not can_fallback:
            raise
        LOGGER.warning(
            "Primary descriptor fetch failed for requested URI; retrying with published result URI."
        )
        try:
            descriptor_bytes = fcp.get_bytes(fallback_descriptor_uri, timeout_s=timeout_s)
        except FCPClientError as fallback_exc:
            raise FCPClientError(
                "Failed to retrieve descriptor from requested URI and published result URI fallback."
            ) from fallback_exc
        resolved_descriptor_uri = fallback_descriptor_uri
        report["descriptor_fetch_fallback_used"] = True
        report["descriptor_fetch_source"] = "published_result_uri"
        report["primary_fetch_error"] = str(primary_exc)
    report["descriptor_uri_resolved"] = resolved_descriptor_uri
    try:
        document = json.loads(descriptor_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Published descriptor is not valid UTF-8 JSON: {exc}") from exc

    if isinstance(document, dict):
        report["descriptor_version"] = document.get("version")
        report["descriptor_release_page_url"] = document.get("release_page_url")

    schema_errors = validate_core_info_document(document)
    report["schema_errors"] = schema_errors

    identity_errors = validate_descriptor_identity(
        document,
        expected_version=expected_version,
        expected_release_page_url=expected_release_page_url,
    )
    report["identity_errors"] = identity_errors

    download_dir = workdir / "downloads"
    if deep:
        download_dir.mkdir(parents=True, exist_ok=True)

    all_retrievable = True
    packages = document.get("packages", {}) if isinstance(document, dict) else {}
    if isinstance(packages, dict):
        for package_key in sorted(packages):
            package_value = packages[package_key]
            if not isinstance(package_value, dict):
                continue
            chk = package_value.get("chk")
            if isinstance(chk, str):
                retrievable = fcp.check_retrievable(chk, timeout_s=timeout_s)
                report["chk_checks"].append(
                    {
                        "kind": "package",
                        "key": package_key,
                        "chk": chk,
                        "retrievable": retrievable,
                    }
                )
                all_retrievable = all_retrievable and retrievable
                if deep and retrievable:
                    payload = fcp.get_bytes(chk, timeout_s=timeout_s)
                    destination = download_dir / f"{_sanitize_filename(package_key)}.bin"
                    destination.write_bytes(payload)

    for field_name in ("changelog_chk", "fullchangelog_chk"):
        chk_uri = document.get(field_name) if isinstance(document, dict) else None
        if isinstance(chk_uri, str):
            retrievable = fcp.check_retrievable(chk_uri, timeout_s=timeout_s)
            report["chk_checks"].append(
                {
                    "kind": "changelog",
                    "key": field_name,
                    "chk": chk_uri,
                    "retrievable": retrievable,
                }
            )
            all_retrievable = all_retrievable and retrievable
            if deep and retrievable:
                payload = fcp.get_bytes(chk_uri, timeout_s=timeout_s)
                destination = download_dir / f"{field_name}.txt"
                destination.write_bytes(payload)

    report["ok"] = not schema_errors and not identity_errors and all_retrievable
    _write_verify_report(workdir, report)
    return report


def validate_descriptor_identity(
    document: Any,
    *,
    expected_version: str | None,
    expected_release_page_url: str | None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(document, dict):
        return errors

    if expected_version:
        actual_version = document.get("version")
        if not isinstance(actual_version, str) or actual_version != expected_version:
            errors.append(
                f"Descriptor version mismatch: expected {expected_version!r}, got {actual_version!r}."
            )

    if expected_release_page_url:
        actual_release_page_url = document.get("release_page_url")
        if (
            not isinstance(actual_release_page_url, str)
            or actual_release_page_url != expected_release_page_url
        ):
            errors.append(
                "Descriptor release_page_url mismatch: "
                f"expected {expected_release_page_url!r}, got {actual_release_page_url!r}."
            )

    return errors


def validate_core_info_document(document: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(document, dict):
        return ["Descriptor root must be a JSON object."]

    version = document.get("version")
    if not isinstance(version, str) or not version:
        errors.append("'version' must be a non-empty string.")

    release_page_url = document.get("release_page_url")
    if not isinstance(release_page_url, str) or not release_page_url:
        errors.append("'release_page_url' must be a non-empty string.")

    for optional_key in ("changelog_chk", "fullchangelog_chk"):
        optional_value = document.get(optional_key)
        if optional_value is not None and not isinstance(optional_value, str):
            errors.append(f"'{optional_key}' must be a string when present.")
        if isinstance(optional_value, str) and not optional_value.startswith(_CHK_PREFIXES):
            errors.append(f"'{optional_key}' must be a CHK URI when present.")

    packages = document.get("packages")
    if not isinstance(packages, dict):
        errors.append("'packages' must be an object.")
        return errors
    if not packages:
        errors.append("'packages' must not be empty.")
        return errors

    for package_key, package_value in packages.items():
        if not isinstance(package_key, str):
            errors.append("All package keys must be strings.")
            continue
        if _PACKAGE_KEY_RE.fullmatch(package_key) is None:
            errors.append(
                f"Package key '{package_key}' must follow <arch>.<ext> with arch amd64|arm64."
            )
            continue
        try:
            validate_package_key(package_key)
        except ValueError as exc:
            errors.append(str(exc))
            continue

        if not isinstance(package_value, dict):
            errors.append(f"Package '{package_key}' value must be an object.")
            continue
        has_chk = "chk" in package_value
        has_store_url = "store_url" in package_value
        if has_chk == has_store_url:
            errors.append(
                f"Package '{package_key}' must contain exactly one of 'chk' or 'store_url'."
            )
        if has_chk:
            chk_uri = package_value.get("chk")
            if not isinstance(chk_uri, str) or not chk_uri.startswith(_CHK_PREFIXES):
                errors.append(f"Package '{package_key}' has invalid 'chk' value.")
        if has_store_url:
            store_url = package_value.get("store_url")
            if not isinstance(store_url, str) or not store_url:
                errors.append(f"Package '{package_key}' has invalid 'store_url' value.")
        if "size" in package_value:
            size = package_value.get("size")
            if not isinstance(size, int) or size < 0:
                errors.append(f"Package '{package_key}' has invalid 'size'; must be >= 0.")

    return errors


def _write_verify_report(workdir: Path, report: dict[str, Any]) -> None:
    save_json_dict(workdir / "verify.json", report)


def _sanitize_filename(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return sanitized or "artifact"
