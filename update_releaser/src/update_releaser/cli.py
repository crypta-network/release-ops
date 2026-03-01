from __future__ import annotations

import argparse
import contextlib
import logging
import os
from pathlib import Path
from typing import Sequence

from update_releaser.fcp_client import FCPClient, FCPClientError
from update_releaser.publish import (
    PUBLISH_TO_PRODUCTION,
    PUBLISH_TO_STAGING,
    publish_revocation,
)
from update_releaser.release_url import parse_release_page_url
from update_releaser.workflow import PutOptions, ReleaseWorkflow, WorkflowError

LOGGER = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    try:
        return args.handler(args)
    except (
        ValueError,
        FileNotFoundError,
        RuntimeError,
        FCPClientError,
        WorkflowError,
    ) as exc:
        LOGGER.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOGGER.error("Interrupted.")
        return 130


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="update-releaser",
        description="Promote Cryptad GitHub release artifacts into update descriptors published over FCP.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity (repeat for more detail).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch_parser = subparsers.add_parser(
        "fetch-assets",
        help="Fetch and cache mapped package assets from a GitHub release.",
    )
    _add_verbose_arg(fetch_parser)
    _add_release_args(fetch_parser)
    fetch_parser.set_defaults(handler=_handle_fetch_assets)

    insert_parser = subparsers.add_parser(
        "insert-artifacts",
        help="Insert downloaded package artifacts via FCP and record CHKs.",
    )
    _add_verbose_arg(insert_parser)
    _add_release_args(insert_parser)
    _add_fcp_args(insert_parser)
    insert_parser.set_defaults(handler=_handle_insert_artifacts)

    changelog_parser = subparsers.add_parser(
        "upload-changelogs",
        help="Upload short/full changelog texts and record changelog CHKs.",
    )
    _add_verbose_arg(changelog_parser)
    _add_release_args(changelog_parser)
    _add_fcp_args(changelog_parser)
    _add_changelog_args(changelog_parser)
    changelog_parser.set_defaults(handler=_handle_upload_changelogs)

    core_info_parser = subparsers.add_parser(
        "generate-core-info",
        help="Generate core-info.json from state.",
    )
    _add_verbose_arg(core_info_parser)
    _add_release_args(core_info_parser)
    core_info_parser.set_defaults(handler=_handle_generate_core_info)

    publish_parser = subparsers.add_parser(
        "publish-descriptor",
        help="Publish core-info.json to either staging OR production update USK.",
    )
    _add_verbose_arg(publish_parser)
    _add_release_args(publish_parser)
    _add_fcp_args(publish_parser)
    _add_publish_args(publish_parser)
    publish_parser.set_defaults(handler=_handle_publish_descriptor)

    verify_parser = subparsers.add_parser(
        "verify",
        help="Fetch published descriptor and verify CHK retrievability with nodata checks.",
    )
    _add_verbose_arg(verify_parser)
    _add_release_args(verify_parser)
    _add_fcp_args(verify_parser)
    _add_publish_args(verify_parser)
    verify_parser.add_argument(
        "--timeout-s",
        type=int,
        default=60,
        help="Timeout in seconds for FCP get/check operations (default: 60).",
    )
    verify_parser.add_argument(
        "--deep",
        action="store_true",
        help="Also download all referenced CHKs into <workdir>/downloads.",
    )
    verify_parser.set_defaults(handler=_handle_verify)

    promote_parser = subparsers.add_parser(
        "promote",
        help="Run fetch -> insert -> changelog upload -> core-info generation -> publish -> verify.",
    )
    _add_verbose_arg(promote_parser)
    _add_release_args(promote_parser)
    _add_fcp_args(promote_parser)
    _add_publish_args(promote_parser)
    _add_changelog_args(promote_parser)
    promote_parser.add_argument(
        "--timeout-s",
        type=int,
        default=60,
        help="Timeout in seconds for verification checks (default: 60).",
    )
    promote_parser.add_argument(
        "--deep",
        action="store_true",
        help="Also download all referenced CHKs during verification.",
    )
    promote_parser.set_defaults(handler=_handle_promote)

    revoke_parser = subparsers.add_parser(
        "revoke",
        help="Publish an emergency revocation message to the revocation SSK.",
    )
    _add_verbose_arg(revoke_parser)
    _add_fcp_args(revoke_parser)
    revoke_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions without inserting revocation data.",
    )
    revoke_parser.add_argument(
        "--revoke-ssk",
        required=True,
        help="Revocation target URI (e.g. SSK@.../revoked).",
    )
    revoke_parser.add_argument(
        "--message",
        required=True,
        help="Revocation message to publish.",
    )
    revoke_parser.set_defaults(handler=_handle_revoke)

    return parser


def _add_verbose_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity (repeat for more detail).",
    )


def _add_release_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "release_url",
        help="GitHub release page URL (e.g. https://github.com/<owner>/<repo>/releases/tag/v1).",
    )
    parser.add_argument(
        "--github-token",
        default=None,
        help="GitHub API token. If omitted, uses GITHUB_TOKEN from the environment.",
    )
    parser.add_argument(
        "--github-source",
        choices=("auto", "api", "gh"),
        default="auto",
        help=(
            "Release fetch backend: auto (API then gh fallback), api (REST only), "
            "or gh (`gh release` commands, useful for draft/untagged releases)."
        ),
    )
    parser.add_argument(
        "--workdir",
        default="./dist",
        help="Base working directory. Tool stores state under <workdir>/<edition>/ (default: ./dist).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned operations without mutating remote services.",
    )


def _add_fcp_args(parser: argparse.ArgumentParser) -> None:
    default_host = os.getenv("FCP_HOST", "127.0.0.1")
    default_port = _env_int("FCP_PORT", 9481)
    parser.add_argument(
        "--fcp-host",
        default=default_host,
        help=f"FCP host (default: {default_host}).",
    )
    parser.add_argument(
        "--fcp-port",
        type=int,
        default=default_port,
        help=f"FCP port (default: {default_port}).",
    )
    parser.add_argument(
        "--priority",
        type=int,
        default=1,
        help="FCP PriorityClass (0 highest, 6 lowest; default: 1).",
    )
    parser.add_argument(
        "--persistence",
        default="forever",
        choices=("connection", "reboot", "forever"),
        help="FCP request persistence mode (default: forever).",
    )
    parser.add_argument(
        "--global-queue",
        dest="global_queue",
        action="store_true",
        default=True,
        help="Insert via FCP global queue (default: enabled).",
    )
    parser.add_argument(
        "--no-global-queue",
        dest="global_queue",
        action="store_false",
        help="Disable FCP global queue.",
    )


def _add_publish_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--publish-to",
        choices=(PUBLISH_TO_STAGING, PUBLISH_TO_PRODUCTION),
        default=PUBLISH_TO_STAGING,
        help="Select exactly one publish target (default: staging).",
    )
    parser.add_argument(
        "--staging-usk-file",
        default="staging-usk.txt",
        help=(
            "Path to staging USK base file (used only when --publish-to staging). "
            "Default resolves from the current working directory. If missing during "
            "publish, a new staging key pair is generated and written here. During "
            "verify, companion *.public files are preferred when this file is private."
        ),
    )


def _add_changelog_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--changelog-file",
        default=None,
        help="Path to short changelog text/markdown file.",
    )
    parser.add_argument(
        "--fullchangelog-file",
        default=None,
        help="Path to full changelog text/markdown file.",
    )


def _handle_fetch_assets(args: argparse.Namespace) -> int:
    workflow = _build_workflow(args)
    assets = workflow.fetch_assets()
    LOGGER.info("Mapped package assets: %d", len(assets))
    return 0


def _handle_insert_artifacts(args: argparse.Namespace) -> int:
    workflow = _build_workflow(args)
    put_options = _build_put_options(args)
    with _fcp_context(args) as fcp:
        packages = workflow.insert_artifacts(fcp=fcp, put_options=put_options)
    LOGGER.info("Package descriptors with CHKs: %d", len(packages))
    return 0


def _handle_upload_changelogs(args: argparse.Namespace) -> int:
    workflow = _build_workflow(args)
    put_options = _build_put_options(args)
    short_override = Path(args.changelog_file) if args.changelog_file else None
    full_override = Path(args.fullchangelog_file) if args.fullchangelog_file else None
    with _fcp_context(args) as fcp:
        changelog_state = workflow.upload_changelogs(
            fcp=fcp,
            put_options=put_options,
            short_override=short_override,
            full_override=full_override,
        )
    LOGGER.info(
        "Uploaded changelog CHKs present: short=%s full=%s",
        bool(changelog_state.get("changelog_chk")),
        bool(changelog_state.get("fullchangelog_chk")),
    )
    return 0


def _handle_generate_core_info(args: argparse.Namespace) -> int:
    workflow = _build_workflow(args)
    core_info_path = workflow.generate_core_info()
    LOGGER.info("Generated core info at %s", core_info_path)
    return 0


def _handle_publish_descriptor(args: argparse.Namespace) -> int:
    workflow = _build_workflow(args)
    put_options = _build_put_options(args)
    staging_usk_file = Path(args.staging_usk_file)
    with _fcp_context(args) as fcp:
        result_uri = workflow.publish_descriptor(
            publish_to=args.publish_to,
            staging_usk_file=staging_usk_file,
            fcp=fcp,
            put_options=put_options,
        )
    LOGGER.info("Published descriptor URI: %s", result_uri)
    return 0


def _handle_verify(args: argparse.Namespace) -> int:
    workflow = _build_workflow(args)
    staging_usk_file = Path(args.staging_usk_file)
    with _fcp_context(args) as fcp:
        report = workflow.verify(
            publish_to=args.publish_to,
            staging_usk_file=staging_usk_file,
            fcp=fcp,
            timeout_s=args.timeout_s,
            deep=args.deep,
        )
    LOGGER.info("Verification status: %s", "ok" if report.get("ok") else "failed")
    return 0 if report.get("ok", False) else 2


def _handle_promote(args: argparse.Namespace) -> int:
    workflow = _build_workflow(args)
    put_options = _build_put_options(args)
    staging_usk_file = Path(args.staging_usk_file)
    short_override = Path(args.changelog_file) if args.changelog_file else None
    full_override = Path(args.fullchangelog_file) if args.fullchangelog_file else None

    workflow.fetch_assets()
    with _fcp_context(args) as fcp:
        workflow.insert_artifacts(fcp=fcp, put_options=put_options)
        workflow.upload_changelogs(
            fcp=fcp,
            put_options=put_options,
            short_override=short_override,
            full_override=full_override,
        )
        core_info_path = workflow.generate_core_info()
        published_uri = workflow.publish_descriptor(
            publish_to=args.publish_to,
            staging_usk_file=staging_usk_file,
            fcp=fcp,
            put_options=put_options,
        )
        report = workflow.verify(
            publish_to=args.publish_to,
            staging_usk_file=staging_usk_file,
            fcp=fcp,
            timeout_s=args.timeout_s,
            deep=args.deep,
        )

    LOGGER.info("Core descriptor: %s", core_info_path)
    LOGGER.info("Publish result URI: %s", published_uri)
    LOGGER.info("Verification status: %s", "ok" if report.get("ok") else "failed")
    return 0 if report.get("ok", False) else 2


def _handle_revoke(args: argparse.Namespace) -> int:
    put_options = _build_put_options(args)
    if args.dry_run:
        LOGGER.info("[dry-run] Would publish revocation message to %s", args.revoke_ssk)
        return 0

    with FCPClient(host=args.fcp_host, port=args.fcp_port, verbosity=max(args.verbose - 1, 0)) as fcp:
        result_uri = publish_revocation(
            fcp,
            revoke_ssk=args.revoke_ssk,
            message=args.message,
            priority=put_options.priority,
            persistence=put_options.persistence,
            global_queue=put_options.global_queue,
        )
    LOGGER.info("Published revocation URI: %s", result_uri)
    return 0


def _build_workflow(args: argparse.Namespace) -> ReleaseWorkflow:
    release_ref = parse_release_page_url(args.release_url)
    return ReleaseWorkflow(
        release_ref=release_ref,
        workdir_base=Path(args.workdir),
        github_token=args.github_token,
        github_source=args.github_source,
        dry_run=bool(args.dry_run),
    )


def _build_put_options(args: argparse.Namespace) -> PutOptions:
    return PutOptions(
        priority=args.priority,
        persistence=args.persistence,
        global_queue=bool(args.global_queue),
    )


def _fcp_context(args: argparse.Namespace):
    if args.dry_run:
        return contextlib.nullcontext(None)
    return FCPClient(host=args.fcp_host, port=args.fcp_port, verbosity=max(args.verbose - 1, 0))


def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


if __name__ == "__main__":
    raise SystemExit(main())
