from __future__ import annotations

from getpass import getpass
import logging
from pathlib import Path

from update_releaser.fcp_client import FCPClient
from update_releaser.state import now_utc_iso

LOGGER = logging.getLogger(__name__)

PUBLISH_TO_STAGING = "staging"
PUBLISH_TO_PRODUCTION = "production"


def validate_usk_base(usk_base: str) -> str:
    normalized = usk_base.strip()
    if not normalized:
        raise ValueError("Update USK base URI is empty.")
    if not normalized.endswith("/info/"):
        raise ValueError("Update USK base must end with '/info/'.")
    return normalized


def read_staging_usk(staging_usk_file: Path) -> str:
    if not staging_usk_file.exists():
        raise FileNotFoundError(
            f"Staging USK file is missing: {staging_usk_file}. "
            "Create this file with a base URI ending in '/info/'."
        )
    content = staging_usk_file.read_text(encoding="utf-8")
    return validate_usk_base(content)


def read_production_usk_from_prompt(*, dry_run: bool) -> str:
    if dry_run:
        return "USK@<production-redacted>/info/"
    entered = getpass("Production update USK base (must end with /info/): ")
    return validate_usk_base(entered)


def resolve_publish_usk_base(
    *,
    publish_to: str,
    staging_usk_file: Path,
    dry_run: bool,
    fcp: FCPClient | None = None,
    auto_generate_staging: bool = False,
    prefer_public_for_staging: bool = False,
) -> str:
    if publish_to == PUBLISH_TO_STAGING:
        if staging_usk_file.exists():
            return _resolve_existing_staging_usk(
                staging_usk_file,
                fcp=fcp,
                prefer_public_for_staging=prefer_public_for_staging,
            )
        if auto_generate_staging:
            if dry_run:
                return "USK@<staging-placeholder>/info/"
            if fcp is None:
                raise ValueError(
                    "FCP client is required to auto-generate a staging USK key pair."
                )
            return generate_staging_usk_file(staging_usk_file, fcp=fcp)
        if dry_run:
            return "USK@<staging-placeholder>/info/"
        return read_staging_usk(staging_usk_file)
    if publish_to == PUBLISH_TO_PRODUCTION:
        return read_production_usk_from_prompt(dry_run=dry_run)
    raise ValueError(f"Unknown publish target: {publish_to}")


def descriptor_target_uri(usk_base: str, edition: str) -> str:
    return f"{validate_usk_base(usk_base)}{edition}"


def publish_descriptor(
    fcp: FCPClient,
    *,
    usk_base: str,
    edition: str,
    core_info_path: Path,
    priority: int,
    persistence: str,
    global_queue: bool,
) -> str:
    target_uri = descriptor_target_uri(usk_base, edition)
    return fcp.put_file_to_uri(
        target_uri,
        core_info_path,
        priority=priority,
        persistence=persistence,
        global_queue=global_queue,
    )


def publish_revocation(
    fcp: FCPClient,
    *,
    revoke_ssk: str,
    message: str,
    priority: int,
    persistence: str,
    global_queue: bool,
) -> str:
    normalized_uri = revoke_ssk.strip()
    if not normalized_uri:
        raise ValueError("Revocation URI (--revoke-ssk) is required.")
    payload = f"{message.strip()}\n\npublished_at={now_utc_iso()}\n".encode("utf-8")
    return fcp.put_bytes_to_uri(
        normalized_uri,
        payload,
        priority=priority,
        persistence=persistence,
        global_queue=global_queue,
    )


def generate_staging_usk_file(staging_usk_file: Path, *, fcp: FCPClient) -> str:
    private_usk_base, public_usk_base = fcp.generate_usk_keypair()
    staging_usk_file.parent.mkdir(parents=True, exist_ok=True)
    _write_usk_file(staging_usk_file, private_usk_base)

    public_usk_file = _derive_public_usk_file(staging_usk_file)
    _write_usk_file(public_usk_file, public_usk_base)

    LOGGER.warning(
        "Staging USK file missing; generated new key pair and wrote private/public files."
    )
    LOGGER.info("Private staging USK file: %s", staging_usk_file)
    LOGGER.info("Public staging USK file: %s", public_usk_file)
    return private_usk_base


def _resolve_existing_staging_usk(
    staging_usk_file: Path,
    *,
    fcp: FCPClient | None,
    prefer_public_for_staging: bool,
) -> str:
    primary = read_staging_usk(staging_usk_file)
    if not prefer_public_for_staging or not _looks_private_staging_usk(primary):
        return primary

    public_usk_file = _derive_public_usk_file(staging_usk_file)
    if public_usk_file.exists():
        LOGGER.info("Using public staging USK companion file: %s", public_usk_file)
        return read_staging_usk(public_usk_file)

    if fcp is not None:
        public_usk_base = fcp.to_public_usk_base(primary)
        _write_usk_file(public_usk_file, public_usk_base)
        LOGGER.warning(
            "Derived public staging USK from private key and wrote companion file: %s",
            public_usk_file,
        )
        return public_usk_base

    LOGGER.warning(
        "Staging USK file appears private and no companion public file was found; using provided key."
    )
    return primary


def _looks_private_staging_usk(usk_base: str) -> bool:
    normalized = usk_base.strip()
    if normalized.startswith("SSK@"):
        return True
    if normalized.startswith("USK@") and ",AQECAAE/" in normalized:
        return True
    return False


def _derive_public_usk_file(staging_usk_file: Path) -> Path:
    if staging_usk_file.suffix:
        return staging_usk_file.with_name(
            f"{staging_usk_file.stem}.public{staging_usk_file.suffix}"
        )
    return staging_usk_file.with_name(f"{staging_usk_file.name}.public")


def _write_usk_file(path: Path, value: str) -> None:
    path.write_text(value.rstrip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        LOGGER.debug("Could not set mode 0600 on %s", path)
