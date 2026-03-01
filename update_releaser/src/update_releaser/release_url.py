from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from urllib.parse import unquote, urlparse

_RELEASE_PATH_RE = re.compile(r"^/([^/]+)/([^/]+)/releases/tag/([^/]+)$")
_NUMERIC_TAG_RE = re.compile(r"^v(\d+)$")
_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class ReleaseRef:
    owner: str
    repo: str
    tag: str
    edition: str
    release_page_url: str


def derive_edition(tag: str) -> str:
    """Derive the updater edition from a Git tag."""
    numeric_match = _NUMERIC_TAG_RE.fullmatch(tag)
    if numeric_match:
        return numeric_match.group(1)
    return sanitize_edition_segment(tag)


def sanitize_edition_segment(tag: str) -> str:
    sanitized = _SAFE_SEGMENT_RE.sub("-", tag.strip())
    sanitized = sanitized.strip(".-_")
    if sanitized:
        return sanitized
    digest = hashlib.sha256(tag.encode("utf-8")).hexdigest()[:12]
    return f"tag-{digest}"


def parse_release_page_url(release_page_url: str) -> ReleaseRef:
    """Parse a GitHub release page URL into owner/repo/tag/edition."""
    if not release_page_url:
        raise ValueError("Release URL is required.")

    parsed = urlparse(release_page_url)
    if parsed.scheme != "https":
        raise ValueError(
            "Invalid release URL: expected https://github.com/<owner>/<repo>/releases/tag/<tag>."
        )
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        raise ValueError(
            "Invalid release URL: expected host github.com in https://github.com/<owner>/<repo>/releases/tag/<tag>."
        )
    if parsed.query or parsed.fragment:
        raise ValueError(
            "Invalid release URL: query string and fragments are not allowed."
        )

    normalized_path = parsed.path.rstrip("/")
    path_match = _RELEASE_PATH_RE.fullmatch(normalized_path)
    if not path_match:
        raise ValueError(
            "Invalid release URL path: expected /<owner>/<repo>/releases/tag/<tag>."
        )

    owner = unquote(path_match.group(1))
    repo = unquote(path_match.group(2))
    tag = unquote(path_match.group(3))
    if not owner or not repo or not tag:
        raise ValueError(
            "Invalid release URL: owner, repo, and tag must all be non-empty."
        )
    if "/" in tag:
        raise ValueError("Invalid release URL: tag may not contain '/'.")

    return ReleaseRef(
        owner=owner,
        repo=repo,
        tag=tag,
        edition=derive_edition(tag),
        release_page_url=release_page_url,
    )
