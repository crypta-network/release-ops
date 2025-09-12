# release-ops: Cryptad Packaging

[![Snap](https://github.com/crypta-network/release-ops/actions/workflows/build-cryptad-snap.yml/badge.svg)](https://github.com/crypta-network/release-ops/actions/workflows/build-cryptad-snap.yml)
[![Flatpak](https://github.com/crypta-network/release-ops/actions/workflows/build-cryptad-flatpak.yml/badge.svg)](https://github.com/crypta-network/release-ops/actions/workflows/build-cryptad-flatpak.yml)
[![Linux Packages](https://github.com/crypta-network/release-ops/actions/workflows/build-cryptad-linux-packages.yml/badge.svg)](https://github.com/crypta-network/release-ops/actions/workflows/build-cryptad-linux-packages.yml)
[![macOS DMG](https://github.com/crypta-network/release-ops/actions/workflows/build-cryptad-macos.yml/badge.svg)](https://github.com/crypta-network/release-ops/actions/workflows/build-cryptad-macos.yml)
[![Windows Installers](https://github.com/crypta-network/release-ops/actions/workflows/build-cryptad-windows.yml/badge.svg)](https://github.com/crypta-network/release-ops/actions/workflows/build-cryptad-windows.yml)

This repository contains CI workflows, reusable composite actions, and packaging
templates to build and ship the Crypta Daemon (cryptad) across multiple
platforms. It focuses on reproducible, per‑architecture builds on native GitHub
runners, lean package payloads, and consistent artifact naming.

Targets covered:
- Snap (amd64, arm64)
- Flatpak (x86_64, aarch64)
- Linux packages via Gradle `jpackage` (deb/rpm, amd64/arm64)
- macOS DMG (arm64)
- Windows installers (amd64, arm64)


## Overview

- Builds source from upstream `crypta-network/cryptad` (checked out under
  `./upstream` during CI).
- Resolves a version automatically (or uses your input), then builds the upstream
  distribution, repacks payloads per platform, renders manifests/templates, and
  produces per‑arch artifacts.
- A manager workflow orchestrates all platforms and drafts a release in the
  upstream repository.


## Quick Start

You run these in GitHub Actions for this repo (no local tools required):

- Snap: Actions → “Build cryptad snap (amd64/arm64)” → Run workflow
- Flatpak: Actions → “Build cryptad flatpak (x86_64/aarch64)” → Run workflow
- Linux packages: Actions → “Build cryptad linux packages (deb/rpm)” → Run workflow
- macOS: Actions → “Build cryptad macOS dmg (arm64)” → Run workflow
- Windows: Actions → “Build cryptad windows installers (amd64/arm64)” → Run workflow
- All + draft release: Actions → “Build all cryptad packages + draft upstream release (v2)”

Inputs for every workflow:
- `version` (optional): when set, upstream ref is `release/<version>` (e.g. 1.2.3)
- `branch` (default `main`): used when `version` is empty

To allow the manager to draft a release in `crypta-network/cryptad`, add a repo
secret `UPSTREAM_REPO_TOKEN` (classic PAT with `repo` scope).


## Version Resolution

If `inputs.version` is empty, the composite action `.github/actions/prepare-cryptad` resolves it in this order:
1) Gradle task `printVersion` (if present)
2) `./gradlew -q properties` → `version:` (if not `unspecified`)
3) `git describe --tags` (leading `v` stripped)

The resolved value (without leading `v`) is used everywhere; packaged versions
for Snap/Flatpak are prefixed with `v` per platform conventions.


## Repository Layout

- `.github/workflows/build-cryptad-snap.yml` — Snap (amd64/arm64) on native runners
- `.github/workflows/build-cryptad-flatpak.yml` — Flatpak (x86_64/aarch64)
- `.github/workflows/build-cryptad-linux-packages.yml` — Gradle `jpackage` (deb/rpm)
- `.github/workflows/build-cryptad-macos.yml` — macOS DMG (arm64, tests disabled)
- `.github/workflows/build-cryptad-windows.yml` — Windows installers (amd64/arm64)
- `.github/workflows/release-cryptad-manager.yml` — Orchestrates all + drafts upstream release
- `.github/actions/prepare-cryptad/` — Checkout upstream, JDK 21, resolve version, Gradle build, expose tarball path
- `.github/actions/repack-payload/` — Extract upstream tarball and re‑tar into platform `local/` dir
- `.github/actions/render-desktop/` — Render shared desktop template (`Exec`/`Icon` substitution)
- `.github/actions/render-manifest/` — Generic token substitution for templates
- `snap/snapcraft.yaml.template` — Snapcraft (core24) template
- `snap/snapshots.yaml` — Snap snapshot exclusion list (installed at `meta/snapshots.yaml`)
- `flatpak/cryptad.yaml.template` — Flatpak manifest (Freedesktop 24.08) template
- `flatpak/network.crypta.cryptad.metainfo.xml.template` — AppStream template
- `desktop/cryptad.desktop.template` — Shared desktop entry for Snap/Flatpak
- `desktop/icons/cryptad.png` and `desktop/icons/cryptad-512.png` — Shared icons (Snap uses PNG; Flatpak installs 512×512)
- `.gitignore` — Ignores generated files, build dirs, rendered manifests


## Snap (core24)

Template: `snap/snapcraft.yaml.template`
- `base: core24`, `grade: stable`, `confinement: strict`, `license: GPL-3.0`
- Per‑arch platforms (native builds):
  - amd64: `build-on [amd64]`, `build-for [amd64]`
  - arm64: `build-on [arm64]`, `build-for [arm64]`
- Part `cryptad` (plugin: `dump`) sources `snap/local/cryptad-jlink-v__VERSION__.tar.gz`
  and pulls a minimal X/desktop stack (`libx11`, `libxext6`, `libfontconfig1`,
  `fonts-dejavu-core`, etc.) plus `bsdutils` (bound as `/usr/bin/script`).
- `prime:` trims docs/man/locales for size.
- `override-prime`:
  - Ensures `bin/cryptad`, `bin/cryptad-launcher`, wrappers, `*/bin/java`, and
    `*/lib/jspawnhelper` are 0755
  - Removes macOS binaries
  - Ships `snap/gui/cryptad.desktop` and `snap/gui/cryptad.png` (Snapcraft includes them under `meta/gui/` automatically)
  - Trims the opposite Linux wrapper based on `CRAFT_TARGET_ARCH`
- `apps.cryptad` and `apps.cryptad-launcher` export GUI/non‑GUI entries with
  GNOME extension and appropriate plugs; `CRYPTAD_ALLOW_ROOT=1` is set.
- The Snap workflow builds on host (no LXD) with `SNAPCRAFT_BUILD_ENVIRONMENT=host`
  and `snapcraft pack --platform <arch>`.

Artifacts:
- GitHub artifact names: `cryptad-snap-<version>-amd64`, `cryptad-snap-<version>-arm64`
- File names: `cryptad-v<version>-<arch>.snap`


## Flatpak (Freedesktop 24.08)

Template: `flatpak/cryptad.yaml.template`
- `id: network.crypta.cryptad`, `branch: v__VERSION__`
- Runtime/SDK: `org.freedesktop.Platform` / `org.freedesktop.Sdk` (24.08)
- GUI command: `cryptad-launcher`; `finish-args` include network, wayland/x11,
  IPC/DRI, and `CRYPTAD_ALLOW_ROOT=1`
- Unpacks `cryptad-jlink-v__VERSION__.tar.gz` to `/app` with `--strip-components=1`
- Post‑install fixes mirror Snap (chmod, remove macOS files, drop non‑target wrapper)
- Installs desktop file and 512×512 hicolor icon, plus AppStream metadata

The workflow installs Flatpak tooling, adds Flathub to the user scope, installs
freedesktop 24.08 runtime+SDK, builds, exports branch `v<version>`, and bundles.

Artifacts:
- GitHub artifact names: `cryptad-flatpak-<version>-amd64`, `cryptad-flatpak-<version>-arm64`
- File names: `cryptad-v<version>-amd64.flatpak`, `cryptad-v<version>-arm64.flatpak`


## Linux Packages (deb/rpm)

Workflow: `.github/workflows/build-cryptad-linux-packages.yml`
- Uses Gradle `jpackage` tasks if available (`jpackageDeb`/`jpackageRpm` or variants);
  falls back to common task names when not explicit.
- Locates `.deb`/`.rpm` outputs (prefers `build/jpackage`), normalizes names, and uploads.

Artifacts (per arch):
- `cryptad-v<version>-<arch>.deb`
- `cryptad-v<version>-<arch>.rpm`


## macOS DMG

Workflow: `.github/workflows/build-cryptad-macos.yml`
- Builds on `macos-latest` arm64 with tests disabled (`run_tests=false`, `-x test`) to avoid
  memory issues on GitHub macOS runners.
- Uploads discovered `.dmg` files; the manager ensures a normalized name
  `cryptad-v<version>-arm64.dmg` in the final release set.


## Windows Installers

Workflow: `.github/workflows/build-cryptad-windows.yml`
- Resolves the upstream ref/version, then calls the reusable workflow at
  `crypta-network/wininstaller-innosetup` to produce amd64 and arm64 installers.
- Collects/renames outputs to standardized names and re‑uploads as artifacts.

Artifacts:
- `CryptaInstaller-v<version>-amd64.exe`
- `CryptaInstaller-v<version>-arm64.exe`


## Manager: Build Everything + Draft Upstream Release

Workflow: `.github/workflows/release-cryptad-manager.yml`
- Calls Snap, Flatpak, Linux, macOS, and Windows workflows via `workflow_call`
- Resolves a final `version` (prefers input; otherwise first child output)
- Downloads all artifacts, filters to standardized names, generates SHA256SUMS,
  and creates a draft release in `crypta-network/cryptad` (requires
  `UPSTREAM_REPO_TOKEN` secret with `repo` scope)

Release payload includes: `.snap`, `.flatpak`, `.deb`, `.rpm`, `.dmg`, Windows
installers, and `SHA256SUMS.txt`.


## Desktop Integration

A shared template `desktop/cryptad.desktop.template` is rendered differently per
platform via the `render-desktop` composite:
- Snap → `Exec=cryptad.cryptad-launcher`, `Icon=${SNAP}/meta/gui/cryptad.png`
- Flatpak → `Exec=cryptad-launcher`, `Icon=network.crypta.cryptad`

Icons:
- Snap reuses `desktop/icons/cryptad.png` at `meta/gui/cryptad.png`
- Flatpak installs `desktop/icons/cryptad-512.png` to hicolor (512×512 required)


## Notes and Rationale

- Native per‑arch builds are more reliable for this project than LXD cross‑builds
  (avoids JRE arch mismatch and LXD network/permission pitfalls).
- Trimming platform‑specific binaries happens in a single place:
  - Snap: `override-prime`
  - Flatpak: post‑install commands
- `.gitignore` excludes all generated/temporary content under `snap/` and
  `flatpak/` that is produced in CI.


## Troubleshooting

- Flatpak: ensure Flathub remote is added in the user scope and freedesktop 24.08
  runtime+SDK are installed before building (the workflow does this).
- Snap: ensure `SNAPCRAFT_BUILD_ENVIRONMENT=host` is set when building on CI.
- If upstream lacks `printVersion` and a project version in Gradle properties,
  CI falls back to `git describe`.


## License

This repository is licensed under GPL-3.0 (see `LICENSE`). The upstream project
`crypta-network/cryptad` is also licensed under GPL-3.0.
