# Release Ops Notes: cryptad Snap Packaging

This document captures the decisions, conventions, and workflow setup used to build multi‑arch (amd64, arm64) Snap packages for the Crypta Daemon (cryptad).

## Goals

- Produce amd64 and arm64 snaps from upstream https://github.com/crypta-network/cryptad.
- Build on native GitHub runners (no LXD) with Snapcraft pack per platform.
- Keep size lean by trimming non‑target assets during prime.
- Upload each arch artifact separately for validation.

## Repository Layout

- `.github/workflows/build-cryptad-snap.yml` — GitHub Actions pipeline.
- `snap/snapcraft.yaml.template` — Snapcraft template rendered with the resolved version.
- `.gitignore` — ignores generated artifacts, snapcraft build dirs, local temp files.

## Snapcraft Template (core24)

Path: `snap/snapcraft.yaml.template`

Key points:
- `base: core24`
- `version: "v__VERSION__"` (snap names include a leading `v`, e.g. `v1.2.3`).
- `confinement: devmode` for test runs. Switch to `strict` for a release.
- `license: GPL-3.0`.
- `platforms` (native‑only build plans):
  - `amd64: build-on [amd64], build-for [amd64]`
  - `arm64: build-on [arm64], build-for [arm64]`
- `parts.cryptad`:
  - `plugin: dump`
  - `source: snap/local/cryptad-jlink-v__VERSION__.tar.gz` (workflow creates this)
  - `stage-packages: [util-linux]` (provides `/usr/bin/script`; JRE is bundled via jlink in the payload)
  - `override-prime`:
    - Runs `craftctl default`.
    - Ensures `bin/cryptad` and native `wrapper*` are `0755`.
    - Removes macOS binaries.
    - Trims the opposite Linux wrapper using `CRAFT_TARGET_ARCH`:
      - On amd64, remove `wrapper-linux-arm-64` and its `.so`.
      - On arm64, remove `wrapper-linux-x86-64` and its `.so`.
- `apps.cryptad`:
  - `command: bin/cryptad`
  - `environment: CRYPTAD_ALLOW_ROOT=1` (snap sandbox runs as root; upstream refuses root without this)
  - `plugs: [network, network-bind]`

## GitHub Actions Workflow

Path: `.github/workflows/build-cryptad-snap.yml`

High‑level flow:
- `workflow_dispatch` inputs:
  - `version` (optional): when set, checkout `release/<version>` from upstream.
  - `branch` (default `main`): branch/tag for upstream when `version` is empty.
- Matrix builds on native runners:
  - `amd64` on `ubuntu-latest`
  - `arm64` on `ubuntu-24.04-arm`
- Steps per job:
  1) Checkout this repository
  2) Decide upstream ref (`release/<version>` or `branch`)
  3) Checkout upstream `crypta-network/cryptad` to `./upstream`
  4) Java setup (Temurin 21) for Gradle
  5) Resolve version (in order):
     - Use `inputs.version` if provided
     - If Gradle task `printVersion` exists: use its output
     - Else `./gradlew -q properties` → `version:` value (if not `unspecified`)
     - Else `git describe --tags` (strip leading `v`)
  6) Build dist: `./gradlew -S -x test build`; expect `build/distributions/cryptad-jlink-v<version>.tar.gz`
     - If not present, auto‑discover `cryptad-*.tar.gz`
     - Write absolute tarball path to `.tarball-path`
  7) Prepare payload:
     - Extract tarball to a work dir
     - Do not trim platform assets here; trimming happens in Snapcraft `override-prime`
     - Repack as `snap/local/cryptad-jlink-v<version>.tar.gz`
  8) Generate `snap/snapcraft.yaml` from template (substitute `__VERSION__`)
  9) Install Snapcraft (`samuelmeuli/action-snapcraft@v3`)
 10) Build snap natively (no LXD):
     - Set `SNAPCRAFT_BUILD_ENVIRONMENT=host`
     - `sudo -E snapcraft pack --platform ${{ matrix.arch }}`
 11) Upload artifact per arch:
     - `cryptad-snap-<version>-amd64` or `cryptad-snap-<version>-arm64`

Diagnostics:
- Uses GitHub Actions `::debug::` and `::group::/::endgroup::` for concise logs.
- Prints Java/Gradle versions, tarball size/SHA256, generated snapcraft.yaml header.

## Rationale and Lessons Learned

- Avoid cross‑arch Snapcraft builds with LXD in CI for this project:
  - We observed platform mis‑selection of JRE and LXD network/permission issues.
  - Building per‑arch on native runners with `pack --platform` and `SNAPCRAFT_BUILD_ENVIRONMENT=host` is more reliable.
- Keep trimming logic in one place:
  - Only Snapcraft `override-prime` removes macOS files and the opposite Linux wrapper.
  - The workflow no longer performs any file pruning.
- Versioning:
  - Snap version is prefixed with `v` to match artifact naming expectations.
- Confinement:
  - `devmode` for testing; switch to `strict` before publishing stable.

## Operational Notes

- Triggering builds:
  - Use the workflow dispatch UI with either:
    - `version = 1.2.3` (checks out upstream `release/1.2.3`), or
    - `branch = some-branch` (when `version` is empty)
- Artifacts:
  - Two separate artifacts per run: one for amd64, one for arm64.
- Upstream dist expectations:
  - Tarball contains `bin/cryptad`, `bin/wrapper-*`, `lib/*`, `conf/wrapper.conf`.
  - The upstream launcher rejects root unless `CRYPTAD_ALLOW_ROOT` is set; the snap sets this env var.

## Future Enhancements

- Switch to `confinement: strict` after testing is complete.
- Optionally run as a daemon:
  - `apps.cryptad.daemon: simple`
  - Provide `restart-condition` and appropriate plugs/slots as needed.
- Auto‑publish to Snap Store:
  - Add `snapcore/action-publish` and store credentials when ready.
- Enrich version detection if upstream adds explicit Gradle `version` fields.

## Known Pitfalls (and Avoided Patterns)

- Don’t use `snapcore/action-build` or LXD cross‑build for this project; it led to wrong JRE arch or network issues.
- Don’t trim platform assets in the workflow; prefer Snapcraft `override-prime` as the single source of truth.
- If Gradle `printVersion` is absent, the workflow falls back to `gradle.properties` → `version:`, then `git describe`.

## Quick References

- Snapcraft docs (core24 platforms):
  https://documentation.ubuntu.com/snapcraft/stable/reference/project-file/snapcraft-yaml/
- Architectures / build plans:
  https://documentation.ubuntu.com/snapcraft/stable/explanation/architectures/
