from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from typing import Any

import httpx

GITHUB_API_BASE = "https://api.github.com"


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    id: int
    name: str
    browser_download_url: str
    size: int
    content_type: str | None = None


@dataclass(frozen=True, slots=True)
class Release:
    id: int
    tag_name: str
    body: str
    assets: tuple[ReleaseAsset, ...]
    raw: dict[str, Any]


class GitHubError(RuntimeError):
    """Raised when GitHub API operations fail."""


def _headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "update_releaser/0.1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def get_release_by_tag(owner: str, repo: str, tag: str, token: str | None) -> Release:
    endpoint = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases/tags/{tag}"
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True) as client:
        response = client.get(endpoint, headers=_headers(token))
    if response.status_code == 404:
        raise GitHubError(
            f"GitHub release not found for {owner}/{repo} tag '{tag}'."
        )
    if response.status_code >= 400:
        message = _extract_error_message(response)
        raise GitHubError(
            f"GitHub API request failed ({response.status_code}) for {owner}/{repo} tag '{tag}': {message}"
        )

    payload = response.json()
    assets: list[ReleaseAsset] = []
    for item in payload.get("assets", []):
        if not isinstance(item, dict):
            continue
        assets.append(
            ReleaseAsset(
                id=int(item.get("id", 0)),
                name=str(item.get("name", "")),
                browser_download_url=str(item.get("browser_download_url", "")),
                size=int(item.get("size", 0)),
                content_type=item.get("content_type"),
            )
        )

    return Release(
        id=int(payload.get("id", 0)),
        tag_name=str(payload.get("tag_name", tag)),
        body=str(payload.get("body") or ""),
        assets=tuple(assets),
        raw=payload,
    )


def get_release_by_tag_gh(owner: str, repo: str, tag: str, token: str | None) -> Release:
    repo_name = f"{owner}/{repo}"
    payload_text = _run_gh_command(
        [
            "release",
            "view",
            tag,
            "--repo",
            repo_name,
            "--json",
            "databaseId,tagName,body,assets,isDraft,isPrerelease,name,publishedAt,url",
        ],
        token=token,
    )
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise GitHubError("Failed to parse JSON from `gh release view`.") from exc

    assets: list[ReleaseAsset] = []
    for index, item in enumerate(payload.get("assets", [])):
        if not isinstance(item, dict):
            continue
        assets.append(
            ReleaseAsset(
                id=_asset_id_from_gh(item, fallback=index + 1),
                name=str(item.get("name", "")),
                browser_download_url=str(item.get("url", "")),
                size=int(item.get("size", 0)),
                content_type=item.get("contentType"),
            )
        )

    return Release(
        id=int(payload.get("databaseId", 0)),
        tag_name=str(payload.get("tagName", tag)),
        body=str(payload.get("body") or ""),
        assets=tuple(assets),
        raw=payload,
    )


def download_asset(browser_download_url: str, dest: Path, token: str | None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest.with_name(dest.name + ".tmp")
    with httpx.Client(timeout=httpx.Timeout(300.0, connect=30.0), follow_redirects=True) as client:
        with client.stream("GET", browser_download_url, headers=_headers(token)) as response:
            if response.status_code >= 400:
                message = _extract_error_message(response)
                raise GitHubError(
                    f"Failed to download asset from {browser_download_url}: {response.status_code} {message}"
                )
            with temp_path.open("wb") as file_handle:
                for chunk in response.iter_bytes():
                    if not chunk:
                        continue
                    file_handle.write(chunk)
    temp_path.replace(dest)


def download_asset_with_gh(
    *,
    owner: str,
    repo: str,
    tag: str,
    asset_name: str,
    dest: Path,
    token: str | None,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest.with_name(dest.name + ".tmp")
    _run_gh_command(
        [
            "release",
            "download",
            tag,
            "--repo",
            f"{owner}/{repo}",
            "--pattern",
            asset_name,
            "--output",
            str(temp_path),
            "--clobber",
        ],
        token=token,
    )
    if not temp_path.exists():
        raise GitHubError(
            f"`gh release download` did not produce an output file for asset {asset_name!r}."
        )
    temp_path.replace(dest)


def save_release_snapshot(release: Release, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(release.raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    dest.write_text(payload, encoding="utf-8")


def _extract_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return response.text.strip() or "unknown error"
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str):
            return message
    return response.text.strip() or "unknown error"


def _asset_id_from_gh(asset: dict[str, Any], *, fallback: int) -> int:
    api_url = asset.get("apiUrl")
    if isinstance(api_url, str):
        tail = api_url.rstrip("/").rsplit("/", 1)[-1]
        if tail.isdigit():
            return int(tail)
    return fallback


def _run_gh_command(args: list[str], *, token: str | None) -> str:
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
    try:
        result = subprocess.run(
            ["gh", *args],
            check=False,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as err:
        raise GitHubError(
            "`gh` CLI is not available in PATH. Install GitHub CLI or use --github-source api."
        ) from err
    if result.returncode != 0:
        stderr = result.stderr.strip() or "unknown gh error"
        raise GitHubError(f"`gh {' '.join(args)}` failed: {stderr}")
    return result.stdout
