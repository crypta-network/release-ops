from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
from typing import Any

from update_releaser.changelog import prepare_changelog_files
from update_releaser.core_info import (
    CoreInfoDescriptor,
    PackageDescriptor,
    write_core_info_files,
)
from update_releaser.fcp_client import FCPClient
from update_releaser.github import (
    GitHubError,
    Release,
    download_asset,
    download_asset_with_gh,
    get_release_by_tag,
    get_release_by_tag_gh,
    save_release_snapshot,
)
from update_releaser.mapping import AssetMappingError, map_release_assets
from update_releaser.publish import (
    PUBLISH_TO_STAGING,
    descriptor_target_uri,
    publish_descriptor as publish_descriptor_to_usk,
    resolve_publish_usk_base,
)
from update_releaser.release_url import ReleaseRef
from update_releaser.state import (
    from_workdir_relative,
    load_json_dict,
    now_utc_iso,
    resolve_workdir,
    save_json_dict,
    sha256_file,
    to_workdir_relative,
)
from update_releaser.verify import verify_published_descriptor

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PutOptions:
    priority: int = 1
    persistence: str = "forever"
    global_queue: bool = True


class WorkflowError(RuntimeError):
    """Raised for workflow-level validation and prerequisite failures."""


class ReleaseWorkflow:
    def __init__(
        self,
        *,
        release_ref: ReleaseRef,
        workdir_base: Path,
        github_token: str | None,
        github_source: str,
        dry_run: bool,
    ):
        self.release_ref = release_ref
        self.workdir = resolve_workdir(workdir_base, release_ref.edition, create=not dry_run)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.workdir / "state.json"
        self.github_token = github_token
        self.github_source = github_source
        self.dry_run = dry_run
        self.state: dict[str, Any] = load_json_dict(self.state_path)
        self._validate_github_source()
        self._ensure_release_identity()

    def fetch_assets(self) -> dict[str, Any]:
        if self.dry_run:
            LOGGER.info(
                "[dry-run] Would fetch GitHub release assets for %s/%s tag %s.",
                self.release_ref.owner,
                self.release_ref.repo,
                self.release_ref.tag,
            )
            return self._state_assets()

        cached_assets = self._state_assets()
        if cached_assets and self._cached_assets_exist(cached_assets):
            LOGGER.info("Reusing previously downloaded assets from %s", self.workdir)
            return cached_assets

        token = self._resolve_github_token()
        release, resolved_source = self._fetch_release(token=token)
        save_release_snapshot(release, self.workdir / "release.json")

        try:
            mapped_assets = map_release_assets(release.assets)
        except AssetMappingError as exc:
            raise WorkflowError(str(exc)) from exc

        asset_records: dict[str, Any] = {}
        asset_dir = self.workdir / "assets"
        asset_dir.mkdir(parents=True, exist_ok=True)

        for package_key in sorted(mapped_assets):
            mapped = mapped_assets[package_key]
            destination = asset_dir / mapped.asset.name
            if destination.exists() and destination.stat().st_size == mapped.asset.size:
                LOGGER.info("Using cached asset %s", destination.name)
            else:
                LOGGER.info("Downloading %s", mapped.asset.name)
                if resolved_source == "gh":
                    download_asset_with_gh(
                        owner=self.release_ref.owner,
                        repo=self.release_ref.repo,
                        tag=self.release_ref.tag,
                        asset_name=mapped.asset.name,
                        dest=destination,
                        token=token,
                    )
                else:
                    download_asset(mapped.asset.browser_download_url, destination, token)

            record_size = destination.stat().st_size
            asset_records[package_key] = {
                "asset_id": mapped.asset.id,
                "asset_name": mapped.asset.name,
                "browser_download_url": mapped.asset.browser_download_url,
                "path": to_workdir_relative(destination, self.workdir),
                "size": record_size,
                "sha256": sha256_file(destination),
            }

        self.state["github_release"] = {
            "id": release.id,
            "tag_name": release.tag_name,
            "source": resolved_source,
            "fetched_at": now_utc_iso(),
        }
        self.state["github_release_body"] = release.body
        self.state["assets"] = asset_records
        self._save_state()
        return asset_records

    def insert_artifacts(self, *, fcp: FCPClient | None, put_options: PutOptions) -> dict[str, Any]:
        if self.dry_run:
            LOGGER.info("[dry-run] Would insert package artifacts as CHKs.")
            return self._state_packages()

        if fcp is None:
            raise WorkflowError("FCP client is required for insert-artifacts.")

        assets = self._state_assets()
        if not assets or not self._cached_assets_exist(assets):
            assets = self.fetch_assets()

        packages = self._state_packages()
        for package_key in sorted(assets):
            asset_record = assets[package_key]
            package_record = packages.get(package_key, {})
            if (
                isinstance(package_record, dict)
                and isinstance(package_record.get("chk"), str)
                and package_record.get("size") == asset_record.get("size")
            ):
                LOGGER.info("Reusing existing CHK for %s", package_key)
                continue

            asset_path = from_workdir_relative(str(asset_record["path"]), self.workdir)
            LOGGER.info("Inserting %s from %s", package_key, asset_path.name)
            chk = fcp.put_file_chk(
                asset_path,
                priority=put_options.priority,
                persistence=put_options.persistence,
                global_queue=put_options.global_queue,
            )
            packages[package_key] = {
                "chk": chk,
                "size": int(asset_record["size"]),
                "asset_name": asset_record["asset_name"],
            }

        self.state["packages"] = packages
        self._save_state()
        return packages

    def upload_changelogs(
        self,
        *,
        fcp: FCPClient | None,
        put_options: PutOptions,
        short_override: Path | None,
        full_override: Path | None,
    ) -> dict[str, Any]:
        if self.dry_run:
            LOGGER.info("[dry-run] Would upload short/full changelog CHKs.")
            return self._state_changelogs()

        if fcp is None:
            raise WorkflowError("FCP client is required for upload-changelogs.")

        release_body = self._ensure_release_body()
        short_path, full_path = prepare_changelog_files(
            self.workdir,
            short_override=short_override,
            full_override=full_override,
            release_body=release_body,
        )

        changelog_state = dict(self._state_changelogs())
        short_sha = sha256_file(short_path)
        full_sha = sha256_file(full_path)
        short_rel_path = to_workdir_relative(short_path, self.workdir)
        full_rel_path = to_workdir_relative(full_path, self.workdir)

        if (
            changelog_state.get("short_sha256") == short_sha
            and isinstance(changelog_state.get("changelog_chk"), str)
        ):
            short_chk = str(changelog_state["changelog_chk"])
            LOGGER.info("Reusing existing short changelog CHK")
        else:
            LOGGER.info("Uploading short changelog from %s", short_path)
            short_chk = fcp.put_file_chk(
                short_path,
                priority=put_options.priority,
                persistence=put_options.persistence,
                global_queue=put_options.global_queue,
            )
            changelog_state.update(
                {
                    "changelog_chk": short_chk,
                    "short_path": short_rel_path,
                    "short_sha256": short_sha,
                }
            )
            self.state["changelogs"] = changelog_state
            self._save_state()
            LOGGER.info("Saved partial changelog state after short upload")

        if (
            changelog_state.get("full_sha256") == full_sha
            and isinstance(changelog_state.get("fullchangelog_chk"), str)
        ):
            full_chk = str(changelog_state["fullchangelog_chk"])
            LOGGER.info("Reusing existing full changelog CHK")
        else:
            LOGGER.info("Uploading full changelog from %s", full_path)
            full_chk = fcp.put_file_chk(
                full_path,
                priority=put_options.priority,
                persistence=put_options.persistence,
                global_queue=put_options.global_queue,
            )
            changelog_state.update(
                {
                    "fullchangelog_chk": full_chk,
                    "full_path": full_rel_path,
                    "full_sha256": full_sha,
                }
            )
            self.state["changelogs"] = changelog_state
            self._save_state()
            LOGGER.info("Saved partial changelog state after full upload")

        updated = {
            "changelog_chk": short_chk,
            "fullchangelog_chk": full_chk,
            "short_path": short_rel_path,
            "full_path": full_rel_path,
            "short_sha256": short_sha,
            "full_sha256": full_sha,
        }
        self.state["changelogs"] = updated
        self._save_state()
        return updated

    def generate_core_info(self) -> Path:
        if self.dry_run:
            target = self.workdir / "core-info.json"
            LOGGER.info("[dry-run] Would generate %s", target)
            return target

        packages_state = self._state_packages()
        if not packages_state:
            raise WorkflowError(
                "No package entries available in state. Run insert-artifacts first."
            )

        package_descriptors: dict[str, PackageDescriptor] = {}
        for package_key in sorted(packages_state):
            package_state = packages_state[package_key]
            if not isinstance(package_state, dict):
                continue
            package_descriptors[package_key] = PackageDescriptor(
                chk=_opt_str(package_state.get("chk")),
                store_url=_opt_str(package_state.get("store_url")),
                size=_opt_int(package_state.get("size")),
            )

        if not package_descriptors:
            raise WorkflowError("Could not build any package descriptors from state.")

        changelog_state = self._state_changelogs()
        descriptor = CoreInfoDescriptor(
            version=self.release_ref.edition,
            release_page_url=self.release_ref.release_page_url,
            changelog_chk=_opt_str(changelog_state.get("changelog_chk")),
            fullchangelog_chk=_opt_str(changelog_state.get("fullchangelog_chk")),
            packages=package_descriptors,
        )
        core_info_path = write_core_info_files(
            descriptor,
            workdir=self.workdir,
            edition=self.release_ref.edition,
        )

        self.state["core_info"] = {
            "path": to_workdir_relative(core_info_path, self.workdir),
            "sha256": sha256_file(core_info_path),
            "generated_at": now_utc_iso(),
        }
        self._save_state()
        return core_info_path

    def publish_descriptor(
        self,
        *,
        publish_to: str,
        staging_usk_file: Path,
        fcp: FCPClient | None,
        put_options: PutOptions,
    ) -> str:
        if not self.dry_run and fcp is None:
            raise WorkflowError("FCP client is required for publish-descriptor.")

        core_info_path = self._core_info_path()
        if core_info_path is None or not core_info_path.exists():
            core_info_path = self.generate_core_info()

        usk_base = resolve_publish_usk_base(
            publish_to=publish_to,
            staging_usk_file=staging_usk_file,
            dry_run=self.dry_run,
            fcp=fcp,
            auto_generate_staging=True,
        )
        target_uri = descriptor_target_uri(usk_base, self.release_ref.edition)

        if self.dry_run:
            LOGGER.info("[dry-run] Would publish descriptor %s to %s", core_info_path, target_uri)
            return target_uri

        core_sha = self.state.get("core_info", {}).get("sha256")
        published_state = self.state.setdefault("published", {})
        existing = published_state.get(publish_to)
        if (
            isinstance(existing, dict)
            and existing.get("descriptor_uri") == target_uri
            and existing.get("core_sha256") == core_sha
            and isinstance(existing.get("result_uri"), str)
        ):
            LOGGER.info("Reusing published descriptor for target %s", publish_to)
            return str(existing["result_uri"])

        assert fcp is not None
        result_uri = publish_descriptor_to_usk(
            fcp,
            usk_base=usk_base,
            edition=self.release_ref.edition,
            core_info_path=core_info_path,
            priority=put_options.priority,
            persistence=put_options.persistence,
            global_queue=put_options.global_queue,
        )
        published_state[publish_to] = {
            "descriptor_uri": target_uri,
            "result_uri": result_uri,
            "core_sha256": core_sha,
            "published_at": now_utc_iso(),
        }
        self.state["published"] = published_state
        self._save_state()
        return result_uri

    def verify(
        self,
        *,
        publish_to: str,
        staging_usk_file: Path,
        fcp: FCPClient | None,
        timeout_s: int,
        deep: bool,
    ) -> dict[str, Any]:
        descriptor_uri = self._descriptor_uri_for_target(
            publish_to=publish_to,
            staging_usk_file=staging_usk_file,
            fcp=fcp,
            prefer_public_for_staging=True,
        )
        fallback_descriptor_uri = self._result_uri_for_target(publish_to=publish_to)
        if not self.dry_run and fcp is None:
            raise WorkflowError("FCP client is required for verify.")

        if self.dry_run:
            report = verify_published_descriptor(
                fcp=FCPClient(host="127.0.0.1", port=9481),
                descriptor_uri=descriptor_uri,
                workdir=self.workdir,
                fallback_descriptor_uri=fallback_descriptor_uri,
                expected_version=self.release_ref.edition,
                expected_release_page_url=self.release_ref.release_page_url,
                timeout_s=timeout_s,
                deep=deep,
                dry_run=True,
            )
            return report

        assert fcp is not None
        report = verify_published_descriptor(
            fcp=fcp,
            descriptor_uri=descriptor_uri,
            workdir=self.workdir,
            fallback_descriptor_uri=fallback_descriptor_uri,
            expected_version=self.release_ref.edition,
            expected_release_page_url=self.release_ref.release_page_url,
            timeout_s=timeout_s,
            deep=deep,
            dry_run=False,
        )
        verification_state = self.state.setdefault("verification", {})
        verification_state[publish_to] = {
            "ok": bool(report.get("ok")),
            "checked_at": report.get("checked_at"),
            "descriptor_uri": descriptor_uri,
            "verify_report": "verify.json",
        }
        self.state["verification"] = verification_state
        self._save_state()
        return report

    def _ensure_release_identity(self) -> None:
        desired = {
            "owner": self.release_ref.owner,
            "repo": self.release_ref.repo,
            "tag": self.release_ref.tag,
            "edition": self.release_ref.edition,
            "release_page_url": self.release_ref.release_page_url,
        }
        existing = self.state.get("release")
        if isinstance(existing, dict):
            for key, expected_value in desired.items():
                current_value = existing.get(key)
                if current_value is not None and current_value != expected_value:
                    raise WorkflowError(
                        f"Existing state at {self.state_path} is for a different release ({key}={current_value!r})."
                    )
        self.state["release"] = desired
        if not self.dry_run:
            self._save_state()

    def _resolve_github_token(self) -> str | None:
        if self.github_token:
            return self.github_token
        env_token = os.getenv("GITHUB_TOKEN")
        if env_token:
            return env_token
        return None

    def _cached_assets_exist(self, assets: dict[str, Any]) -> bool:
        for package_key, record in assets.items():
            if not isinstance(package_key, str) or not isinstance(record, dict):
                return False
            relative_path = record.get("path")
            if not isinstance(relative_path, str):
                return False
            asset_path = from_workdir_relative(relative_path, self.workdir)
            if not asset_path.exists():
                return False
        return True

    def _ensure_release_body(self) -> str:
        cached = self.state.get("github_release_body")
        if isinstance(cached, str) and cached:
            return cached
        if self.dry_run:
            return ""
        token = self._resolve_github_token()
        release, resolved_source = self._fetch_release(token=token)
        save_release_snapshot(release, self.workdir / "release.json")
        self.state["github_release"] = {
            "id": release.id,
            "tag_name": release.tag_name,
            "source": resolved_source,
            "fetched_at": now_utc_iso(),
        }
        self.state["github_release_body"] = release.body
        self._save_state()
        return release.body

    def _validate_github_source(self) -> None:
        if self.github_source not in {"api", "gh", "auto"}:
            raise WorkflowError(
                f"Invalid --github-source value {self.github_source!r}. "
                "Expected one of: api, gh, auto."
            )

    def _fetch_release(self, *, token: str | None) -> tuple[Release, str]:
        owner = self.release_ref.owner
        repo = self.release_ref.repo
        tag = self.release_ref.tag

        if self.github_source == "api":
            release = get_release_by_tag(owner, repo, tag, token)
            return release, "api"
        if self.github_source == "gh":
            release = get_release_by_tag_gh(owner, repo, tag, token)
            return release, "gh"

        try:
            release = get_release_by_tag(owner, repo, tag, token)
            return release, "api"
        except GitHubError as api_error:
            LOGGER.warning("GitHub API lookup failed; attempting `gh` fallback: %s", api_error)
            try:
                release = get_release_by_tag_gh(owner, repo, tag, token)
                return release, "gh"
            except GitHubError as gh_error:
                message = (
                    "Failed to fetch release via both GitHub API and `gh` CLI. "
                    f"API error: {api_error}. gh error: {gh_error}."
                )
                raise WorkflowError(message) from gh_error

    def _core_info_path(self) -> Path | None:
        core_info_state = self.state.get("core_info")
        if not isinstance(core_info_state, dict):
            return None
        raw_path = core_info_state.get("path")
        if not isinstance(raw_path, str):
            return None
        return from_workdir_relative(raw_path, self.workdir)

    def _descriptor_uri_for_target(
        self,
        *,
        publish_to: str,
        staging_usk_file: Path,
        fcp: FCPClient | None,
        prefer_public_for_staging: bool,
    ) -> str:
        if publish_to == PUBLISH_TO_STAGING and prefer_public_for_staging:
            usk_base = resolve_publish_usk_base(
                publish_to=publish_to,
                staging_usk_file=staging_usk_file,
                dry_run=self.dry_run,
                fcp=fcp,
                auto_generate_staging=False,
                prefer_public_for_staging=True,
            )
            return descriptor_target_uri(usk_base, self.release_ref.edition)

        published = self.state.get("published", {})
        if isinstance(published, dict):
            target_state = published.get(publish_to)
            if isinstance(target_state, dict):
                descriptor_uri = target_state.get("descriptor_uri")
                if isinstance(descriptor_uri, str) and descriptor_uri:
                    return descriptor_uri

        usk_base = resolve_publish_usk_base(
            publish_to=publish_to,
            staging_usk_file=staging_usk_file,
            dry_run=self.dry_run,
        )
        return descriptor_target_uri(usk_base, self.release_ref.edition)

    def _result_uri_for_target(self, *, publish_to: str) -> str | None:
        published = self.state.get("published", {})
        if not isinstance(published, dict):
            return None
        target_state = published.get(publish_to)
        if not isinstance(target_state, dict):
            return None
        result_uri = target_state.get("result_uri")
        if not isinstance(result_uri, str) or not result_uri:
            return None
        return result_uri

    def _save_state(self) -> None:
        if self.dry_run:
            return
        save_json_dict(self.state_path, self.state)

    def _state_assets(self) -> dict[str, Any]:
        value = self.state.get("assets")
        return value if isinstance(value, dict) else {}

    def _state_packages(self) -> dict[str, Any]:
        value = self.state.get("packages")
        return value if isinstance(value, dict) else {}

    def _state_changelogs(self) -> dict[str, Any]:
        value = self.state.get("changelogs")
        return value if isinstance(value, dict) else {}


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _opt_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None
