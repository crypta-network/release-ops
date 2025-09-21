#!/usr/bin/env python3
"""Upsert the SHA256 Checksums section within an existing release body."""

from __future__ import annotations

import pathlib
import re
import sys


def main(args: list[str]) -> int:
    if len(args) != 3:
        sys.stderr.write(
            "Usage: upsert_sha256_section.py <existing_body> <section_fragment> <output_body>\n"
        )
        return 1

    existing_path = pathlib.Path(args[0])
    section_path = pathlib.Path(args[1])
    output_path = pathlib.Path(args[2])

    try:
        section_lines = section_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        sys.stderr.write(f"Missing section fragment: {section_path}\n")
        return 1

    if existing_path.exists():
        existing_lines = existing_path.read_text(encoding="utf-8").splitlines()
    else:
        existing_lines = []

    header_re = re.compile(r"^##\s+SHA256\s+Checksums\s*$")

    result: list[str] = []
    i = 0
    found = False

    while i < len(existing_lines):
        line = existing_lines[i]
        if not found and header_re.match(line):
            print("::debug::Existing body contains SHA256 section; updating it")
            result.extend(section_lines)
            i += 1
            while i < len(existing_lines) and existing_lines[i].strip() == "":
                i += 1
            if i < len(existing_lines) and existing_lines[i].strip().startswith("```"):
                i += 1
                while i < len(existing_lines):
                    if existing_lines[i].strip().startswith("```"):
                        i += 1
                        break
                    i += 1
            else:
                while (
                    i < len(existing_lines)
                    and existing_lines[i].strip() != ""
                    and not existing_lines[i].startswith("##")
                ):
                    i += 1
            while i < len(existing_lines) and existing_lines[i].strip() == "":
                i += 1
            if i < len(existing_lines):
                result.append("")
            found = True
            continue
        result.append(line)
        i += 1

    if not found:
        print("::debug::No SHA256 section; appending new section")
        if result and result[-1].strip() != "":
            result.append("")
        result.extend(section_lines)

    while result and result[-1] == "":
        result.pop()

    output_path.write_text("\n".join(result) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
