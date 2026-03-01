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
_CHANGELOG_CHK_FIELDS = ("changelog_chk", "fullchangelog_chk")


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
    report = _new_verify_report(descriptor_uri=descriptor_uri, deep=deep, dry_run=dry_run)

    if dry_run:
        report["ok"] = True
        _write_verify_report(workdir, report)
        return report

    descriptor_bytes, resolved_descriptor_uri = _fetch_descriptor_bytes(
        fcp=fcp,
        descriptor_uri=descriptor_uri,
        fallback_descriptor_uri=fallback_descriptor_uri,
        timeout_s=timeout_s,
        report=report,
    )
    report["descriptor_uri_resolved"] = resolved_descriptor_uri

    document = _parse_descriptor_json(descriptor_bytes)
    _set_descriptor_identity_fields(report, document)

    schema_errors = validate_core_info_document(document)
    report["schema_errors"] = schema_errors

    identity_errors = validate_descriptor_identity(
        document,
        expected_version=expected_version,
        expected_release_page_url=expected_release_page_url,
    )
    report["identity_errors"] = identity_errors

    download_dir = _prepare_download_dir(workdir, deep=deep)
    all_retrievable = _verify_all_chk_references(
        fcp=fcp,
        document=document,
        report=report,
        timeout_s=timeout_s,
        download_dir=download_dir,
    )

    report["ok"] = not schema_errors and not identity_errors and all_retrievable
    _write_verify_report(workdir, report)
    return report


def _new_verify_report(*, descriptor_uri: str, deep: bool, dry_run: bool) -> dict[str, Any]:
    return {
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


def _fetch_descriptor_bytes(
    *,
    fcp: FCPClient,
    descriptor_uri: str,
    fallback_descriptor_uri: str | None,
    timeout_s: int,
    report: dict[str, Any],
) -> tuple[bytes, str]:
    try:
        return fcp.get_bytes(descriptor_uri, timeout_s=timeout_s), descriptor_uri
    except FCPClientError as primary_exc:
        if not _can_fallback_descriptor_uri(
            descriptor_uri=descriptor_uri, fallback_descriptor_uri=fallback_descriptor_uri
        ):
            raise

        LOGGER.warning(
            "Primary descriptor fetch failed for requested URI; retrying with published result URI."
        )
        descriptor_bytes = _fetch_descriptor_with_fallback(
            fcp=fcp,
            fallback_descriptor_uri=fallback_descriptor_uri,
            timeout_s=timeout_s,
        )
        report["descriptor_fetch_fallback_used"] = True
        report["descriptor_fetch_source"] = "published_result_uri"
        report["primary_fetch_error"] = str(primary_exc)
        assert fallback_descriptor_uri is not None
        return descriptor_bytes, fallback_descriptor_uri


def _can_fallback_descriptor_uri(*, descriptor_uri: str, fallback_descriptor_uri: str | None) -> bool:
    return bool(fallback_descriptor_uri and fallback_descriptor_uri != descriptor_uri)


def _fetch_descriptor_with_fallback(
    *,
    fcp: FCPClient,
    fallback_descriptor_uri: str | None,
    timeout_s: int,
) -> bytes:
    assert fallback_descriptor_uri is not None
    try:
        return fcp.get_bytes(fallback_descriptor_uri, timeout_s=timeout_s)
    except FCPClientError as fallback_exc:
        raise FCPClientError(
            "Failed to retrieve descriptor from requested URI and published result URI fallback."
        ) from fallback_exc


def _parse_descriptor_json(descriptor_bytes: bytes) -> Any:
    try:
        return json.loads(descriptor_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Published descriptor is not valid UTF-8 JSON: {exc}") from exc


def _set_descriptor_identity_fields(report: dict[str, Any], document: Any) -> None:
    if not isinstance(document, dict):
        return
    report["descriptor_version"] = document.get("version")
    report["descriptor_release_page_url"] = document.get("release_page_url")


def _prepare_download_dir(workdir: Path, *, deep: bool) -> Path | None:
    if not deep:
        return None
    download_dir = workdir / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    return download_dir


def _verify_all_chk_references(
    *,
    fcp: FCPClient,
    document: Any,
    report: dict[str, Any],
    timeout_s: int,
    download_dir: Path | None,
) -> bool:
    all_retrievable = True

    for package_key, chk_uri in _iter_package_chk_references(document):
        destination = (
            download_dir / f"{_sanitize_filename(package_key)}.bin"
            if download_dir is not None
            else None
        )
        retrievable = _check_and_record_chk(
            fcp=fcp,
            report=report,
            timeout_s=timeout_s,
            kind="package",
            key=package_key,
            chk_uri=chk_uri,
            destination=destination,
        )
        all_retrievable = all_retrievable and retrievable

    for field_name, chk_uri in _iter_changelog_chk_references(document):
        destination = download_dir / f"{field_name}.txt" if download_dir is not None else None
        retrievable = _check_and_record_chk(
            fcp=fcp,
            report=report,
            timeout_s=timeout_s,
            kind="changelog",
            key=field_name,
            chk_uri=chk_uri,
            destination=destination,
        )
        all_retrievable = all_retrievable and retrievable

    return all_retrievable


def _iter_package_chk_references(document: Any) -> list[tuple[str, str]]:
    if not isinstance(document, dict):
        return []
    packages = document.get("packages")
    if not isinstance(packages, dict):
        return []

    references: list[tuple[str, str]] = []
    for package_key in sorted(packages):
        package_value = packages[package_key]
        if not isinstance(package_value, dict):
            continue
        chk_uri = package_value.get("chk")
        if isinstance(chk_uri, str):
            references.append((package_key, chk_uri))
    return references


def _iter_changelog_chk_references(document: Any) -> list[tuple[str, str]]:
    if not isinstance(document, dict):
        return []

    references: list[tuple[str, str]] = []
    for field_name in _CHANGELOG_CHK_FIELDS:
        chk_uri = document.get(field_name)
        if isinstance(chk_uri, str):
            references.append((field_name, chk_uri))
    return references


def _check_and_record_chk(
    *,
    fcp: FCPClient,
    report: dict[str, Any],
    timeout_s: int,
    kind: str,
    key: str,
    chk_uri: str,
    destination: Path | None,
) -> bool:
    retrievable = fcp.check_retrievable(chk_uri, timeout_s=timeout_s)
    report["chk_checks"].append(
        {
            "kind": kind,
            "key": key,
            "chk": chk_uri,
            "retrievable": retrievable,
        }
    )
    if destination is not None and retrievable:
        payload = fcp.get_bytes(chk_uri, timeout_s=timeout_s)
        destination.write_bytes(payload)
    return retrievable


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
    if not isinstance(document, dict):
        return ["Descriptor root must be a JSON object."]

    errors: list[str] = []
    errors.extend(_validate_descriptor_required_fields(document))
    errors.extend(_validate_optional_changelog_chk_fields(document))

    packages = document.get("packages")
    package_errors, normalized_packages = _validate_packages_collection(packages)
    errors.extend(package_errors)
    if normalized_packages is None:
        return errors

    for package_key, package_value in normalized_packages.items():
        errors.extend(_validate_package_entry(package_key=package_key, package_value=package_value))
    return errors


def _validate_descriptor_required_fields(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    version = document.get("version")
    if not _is_non_empty_string(version):
        errors.append("'version' must be a non-empty string.")

    release_page_url = document.get("release_page_url")
    if not _is_non_empty_string(release_page_url):
        errors.append("'release_page_url' must be a non-empty string.")

    return errors


def _validate_optional_changelog_chk_fields(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for optional_key in _CHANGELOG_CHK_FIELDS:
        optional_value = document.get(optional_key)
        if optional_value is not None and not isinstance(optional_value, str):
            errors.append(f"'{optional_key}' must be a string when present.")
            continue
        if isinstance(optional_value, str) and not _is_chk_uri(optional_value):
            errors.append(f"'{optional_key}' must be a CHK URI when present.")
    return errors


def _validate_packages_collection(packages: Any) -> tuple[list[str], dict[Any, Any] | None]:
    if not isinstance(packages, dict):
        return ["'packages' must be an object."], None
    if not packages:
        return ["'packages' must not be empty."], None
    return [], packages


def _validate_package_entry(*, package_key: Any, package_value: Any) -> list[str]:
    normalized_key, key_errors = _normalize_and_validate_package_key(package_key)
    if normalized_key is None:
        return key_errors
    if not isinstance(package_value, dict):
        return [*key_errors, f"Package '{normalized_key}' value must be an object."]
    return [*key_errors, *_validate_package_payload(package_key=normalized_key, package_value=package_value)]


def _normalize_and_validate_package_key(package_key: Any) -> tuple[str | None, list[str]]:
    errors: list[str] = []
    if not isinstance(package_key, str):
        errors.append("All package keys must be strings.")
        return None, errors

    if _PACKAGE_KEY_RE.fullmatch(package_key) is None:
        errors.append(
            f"Package key '{package_key}' must follow <arch>.<ext> with arch amd64|arm64."
        )
        return None, errors

    try:
        validate_package_key(package_key)
    except ValueError as exc:
        errors.append(str(exc))
        return None, errors

    return package_key, errors


def _validate_package_payload(*, package_key: str, package_value: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    has_chk = "chk" in package_value
    has_store_url = "store_url" in package_value

    if has_chk == has_store_url:
        errors.append(
            f"Package '{package_key}' must contain exactly one of 'chk' or 'store_url'."
        )
    if has_chk and not _is_chk_uri(package_value.get("chk")):
        errors.append(f"Package '{package_key}' has invalid 'chk' value.")
    if has_store_url and not _is_non_empty_string(package_value.get("store_url")):
        errors.append(f"Package '{package_key}' has invalid 'store_url' value.")
    if "size" in package_value and not _is_non_negative_int(package_value.get("size")):
        errors.append(f"Package '{package_key}' has invalid 'size'; must be >= 0.")

    return errors


def _is_chk_uri(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_CHK_PREFIXES)


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and value >= 0


def _write_verify_report(workdir: Path, report: dict[str, Any]) -> None:
    save_json_dict(workdir / "verify.json", report)


def _sanitize_filename(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return sanitized or "artifact"
