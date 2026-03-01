from __future__ import annotations

from pathlib import Path


def derive_changelog_texts(release_body: str) -> tuple[str, str]:
    normalized_body = (release_body or "").strip()
    if not normalized_body:
        fallback = "No changelog content was provided in the GitHub release body."
        return f"{fallback}\n", f"{fallback}\n"

    lines = normalized_body.splitlines()
    short_lines = _first_section(lines)
    if not short_lines:
        short_lines = lines[:20]
    if len(short_lines) > 20:
        short_lines = short_lines[:20]

    short_text = "\n".join(short_lines).strip() + "\n"
    full_text = normalized_body + "\n"
    return short_text, full_text


def prepare_changelog_files(
    workdir: Path,
    *,
    short_override: Path | None,
    full_override: Path | None,
    release_body: str,
) -> tuple[Path, Path]:
    workdir.mkdir(parents=True, exist_ok=True)

    if short_override:
        short_text = short_override.read_text(encoding="utf-8")
    else:
        short_text, _ = derive_changelog_texts(release_body)

    if full_override:
        full_text = full_override.read_text(encoding="utf-8")
    else:
        _, full_text = derive_changelog_texts(release_body)

    short_path = workdir / "changelog-short.md"
    full_path = workdir / "changelog-full.md"
    short_path.write_text(_ensure_ending_newline(short_text), encoding="utf-8")
    full_path.write_text(_ensure_ending_newline(full_text), encoding="utf-8")
    return short_path, full_path


def _first_section(lines: list[str]) -> list[str]:
    section: list[str] = []
    started = False
    for line in lines:
        if not started and not line.strip():
            continue
        if line.startswith("#") and started and section:
            break
        started = True
        section.append(line)
    return section


def _ensure_ending_newline(text: str) -> str:
    return text if text.endswith("\n") else f"{text}\n"
