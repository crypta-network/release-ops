# Release Ops Notes: cryptad Packaging (Snap + Flatpak)

This document captures the decisions, conventions, and workflow setup used to build multi‑arch packages for the Crypta Daemon (cryptad).

It now covers both Snap (amd64, arm64) and Flatpak (x86_64, aarch64).

## Goals

- Produce amd64 and arm64 snaps from upstream https://github.com/crypta-network/cryptad.
- Produce x86_64 and aarch64 Flatpak bundles from the same upstream artifacts.
- Build on native GitHub runners (no LXD) using per‑platform packaging.
- Keep size lean by trimming non‑target assets during package prime/install stages.
- Upload each arch artifact separately for validation.

## Repository Layout

- `.github/workflows/build-cryptad-snap.yml` — GitHub Actions pipeline (Snap).
- `.github/workflows/build-cryptad-flatpak.yml` — GitHub Actions pipeline (Flatpak).
- `.github/workflows/build-cryptad-jar.yml` — GitHub Actions pipeline (plain JAR).
- `.github/actions/prepare-cryptad/` — composite for upstream checkout + version resolve + Gradle dist.
- `.github/actions/repack-payload/` — composite to extract upstream tarball and re‑tar into target `local/` dir.
- `.github/actions/render-desktop/` — composite to render shared desktop template (Exec/Icon substitutions).
- `.github/actions/render-manifest/` — composite to render templates with KEY=VALUE substitutions.
- `snap/snapcraft.yaml.template` — Snapcraft template rendered with the resolved version.
- `snap/snapshots.yaml` — Snap snapshots exclusions included in the snap at `meta/snapshots.yaml`.
- `flatpak/cryptad.yaml.template` — Flatpak manifest template rendered with the resolved version.
- `flatpak/network.crypta.cryptad.metainfo.xml.template` — AppStream metadata template (rendered and included in Flatpak).
- `desktop/cryptad.desktop.template` — shared desktop entry template used by both Snap and Flatpak workflows.
- `desktop/icons/cryptad.png` and `desktop/icons/cryptad-512.png` — shared app icons (Snap uses the PNG; Flatpak installs 512×512 to hicolor).
- `.gitignore` — ignores generated artifacts, build dirs, and rendered files for both packaging flows.

## Snapcraft Template (core24)

Path: `snap/snapcraft.yaml.template`

Key points:
- `base: core24`
- `version: "v__VERSION__"` (snap names include a leading `v`, e.g. `v1.2.3`).
- `grade: stable`, `confinement: strict`.
- `license: GPL-3.0`.
- `platforms` (native‑only build plans):
  - `amd64: build-on [amd64], build-for [amd64]`
  - `arm64: build-on [arm64], build-for [arm64]`
- `parts.cryptad`:
  - `plugin: dump`
  - `source: snap/local/cryptad-jlink-v__VERSION__.tar.gz` (workflow creates this)
  - `stage-packages` includes a minimal desktop/X stack plus fonts for Swing UIs and a `script` provider. Current set:
    - `bsdutils` (binds `/usr/bin/script` via `layout:`)
    - `libx11-6`, `libxext6`, `libxrender1`, `libxtst6`, `libxi6`, `libxcb1`, `libxau6`, `libxdmcp6`
    - `libfontconfig1`, `libfreetype6`, `fonts-dejavu-core`
  - `prime:` trims docs/manpages/locales to keep size lean.
- `override-prime`:
  - Runs `craftctl default`.
  - Ensures `bin/cryptad`, `bin/cryptad-launcher`, native `wrapper*` are `0755`.
  - Ensures any bundled JRE entrypoint (`*/bin/java`) and `*/lib/jspawnhelper` are `0755`.
  - Removes macOS binaries.
  - Do not copy the `.desktop` or icon manually. We ship `snap/gui/cryptad.desktop` and `snap/gui/cryptad.png`, which Snapcraft includes under `meta/gui/` automatically.
  - Trims the opposite Linux wrapper using `CRAFT_TARGET_ARCH`:
      - On amd64, remove `wrapper-linux-arm-64` and its `.so`.
      - On arm64, remove `wrapper-linux-x86-64` and its `.so`.
- Additional part `snapshot-exclusions` (`plugin: nil`) installs `meta/snapshots.yaml` from `snap/snapshots.yaml` to exclude sensitive/cache data from Snap snapshots.
- `layout:` binds `/usr/bin/script` inside the snap to the packaged binary for compatibility.
- `apps.cryptad`:
  - `command: bin/cryptad`
  - `environment:` sets `CRYPTAD_ALLOW_ROOT=1`, extends `PATH`, and configures `JAVA_TOOL_OPTIONS` (tmp dirs, user.home to `$SNAP_USER_COMMON`).
  - `plugs:` includes `home`, `removable-media`, `network`, `network-bind`.
- `apps.cryptad-launcher` (GUI entry):
  - `command: bin/cryptad-launcher`, `extensions: [ gnome ]` (no `desktop:` key; using a `.desktop` in `snap/gui/` avoids duplication).
  - `environment:` same as `cryptad`.
  - `plugs:` adds GUI integration (`wayland`, `x11`, `desktop`, `desktop-legacy`) plus `home`, `network`, `network-bind`, and optional `removable-media`.

## GitHub Actions Workflow (Snap)

Path: `.github/workflows/build-cryptad-snap.yml`

High‑level flow:
- `workflow_dispatch` inputs:
  - `version` (optional): when set, checkout `release/<version>` from upstream.
  - `branch` (default `main`): branch/tag for upstream when `version` is empty.
- Matrix builds on native runners:
  - `amd64` on `ubuntu-latest`
  - `arm64` on `ubuntu-24.04-arm`
- Refactor note (2025‑09‑01): this workflow now calls composite actions
  to deduplicate the upstream build, repack, and template rendering.
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
     - Extract tarball to a work dir; no pruning here (trimming happens in Snapcraft `override-prime`).
     - Repack as `snap/local/cryptad-jlink-v<version>.tar.gz`.
 8) Prepare desktop assets:
     - Copy shared icon `desktop/icons/cryptad.png` to `snap/gui/cryptad.png`.
     - Render `snap/gui/cryptad.desktop` from the shared template with `Exec=cryptad.cryptad-launcher`.
  9) Generate `snap/snapcraft.yaml` from template (substitute `__VERSION__`).
 10) Install Snapcraft (`samuelmeuli/action-snapcraft@v3`).
 11) Build snap natively (no LXD):
     - Set `SNAPCRAFT_BUILD_ENVIRONMENT=host`.
     - Run `sudo -E snapcraft pack --platform ${{ matrix.arch }}`.
 12) Upload artifact per arch:
     - `cryptad-snap-<version>-amd64` or `cryptad-snap-<version>-arm64`.

Diagnostics:
- Uses GitHub Actions `::debug::` and `::group::/::endgroup::` for concise logs.
- Prints Java/Gradle versions, tarball size/SHA256, and generated `snapcraft.yaml` header.

## Flatpak Template (Freedesktop 24.08)

Path: `flatpak/cryptad.yaml.template`

Key points:
- `id: network.crypta.cryptad`, `branch: v__VERSION__`.
- `runtime: org.freedesktop.Platform`, `runtime-version: '24.08'`, `sdk: org.freedesktop.Sdk`.
- `command: cryptad-launcher` (GUI entry) with `finish-args` for network, wayland/x11, IPC, DRI, and `env: CRYPTAD_ALLOW_ROOT=1`.
- Single `modules` entry unpacks `cryptad-jlink-v__VERSION__.tar.gz` to `/app` with `--strip-components=1` to ensure `/app/bin/cryptad` exists.
- Post‑install fixes mirror Snap trim logic:
  - `chmod 0755` for `bin/cryptad`, `bin/cryptad-launcher`, any `*/bin/java`, and `*/lib/jspawnhelper`.
  - Remove macOS binaries and non‑target wrappers based on `FLATPAK_ARCH`.
- Installs desktop entry `cryptad.desktop` and 512×512 icon to hicolor, plus AppStream metadata.

## GitHub Actions Workflow (Flatpak)

Path: `.github/workflows/build-cryptad-flatpak.yml`

High‑level flow:
- `workflow_dispatch` inputs mirror Snap (`version` or `branch`).
- Matrix on native runners:
  - `x86_64` on `ubuntu-latest`
  - `aarch64` on `ubuntu-24.04-arm`
- Refactor note (2025‑09‑01): this workflow now calls composite actions
  for upstream prep, repack, and template rendering.

## Reusable Composite Actions

- `./.github/actions/prepare-cryptad`
  - Inputs: `version`, `branch`, `upstream_repository?`, `upstream_path?`.
  - Outputs: `version`, `tarball_path` (absolute). Builds upstream with Gradle and locates the dist tarball.
- `./.github/actions/repack-payload`
  - Inputs: `tarball_path`, `version`, `output_dir`.
  - Outputs: `output_tarball`. Re‑tars upstream payload to `snap/local/` or `flatpak/local/`.
- `./.github/actions/render-desktop`
  - Inputs: `template_path`, `exec_name`, `icon_name`, `output_path`.
  - Purpose: Renders `.desktop` from shared template for Snap/Flatpak.
- `./.github/actions/render-manifest`
  - Inputs: `template_path`, `output_path`, `substitutions` (newline KEY=VALUE), `preview_lines?`.
  - Purpose: Generic token substitution for `snapcraft.yaml`, Flatpak manifest, and metainfo.
- Steps per job:
  1) Checkout this repo and select upstream ref.
  2) Checkout upstream `crypta-network/cryptad` to `./upstream`.
  3) Java setup (Temurin 21), resolve version (same heuristics as Snap).
  4) Build upstream dist; write absolute tarball path to `.tarball-path`.
  5) Repack payload to `flatpak/local/cryptad-jlink-v<version>.tar.gz`.
  6) Render `flatpak/cryptad.yaml` and `flatpak/network.crypta.cryptad.metainfo.xml` from templates.
  7) Render shared desktop file (`Exec=cryptad-launcher`, `Icon=network.crypta.cryptad`).
  8) Install Flatpak tooling; add Flathub remote to the user scope; install freedesktop runtime+SDK 24.08.
  9) Build with `flatpak-builder --user --arch=<matrix>`.
 10) Export repo to branch `v<version>` and bundle as `cryptad-flatpak-v<version>-<arch>.flatpak`.
 11) Upload artifact per arch: `cryptad-flatpak-<version>-x86_64` or `...-aarch64`.

## Rationale and Lessons Learned

- Avoid cross‑arch Snapcraft builds with LXD in CI for this project:
  - We observed platform mis‑selection of JRE and LXD network/permission issues.
  - Building per‑arch on native runners with `pack --platform` and `SNAPCRAFT_BUILD_ENVIRONMENT=host` is more reliable.
- Keep trimming logic in one place:
  - Only Snapcraft `override-prime` removes macOS files and the opposite Linux wrapper.
  - The workflow no longer performs any file pruning.
- Deduplicate CI logic via composites:
  - Upstream prep, payload repack, and template rendering are shared across Snap/Flatpak workflows.
  - Keeps jobs small, declarative, and easier to evolve per‑platform.
- Versioning:
  - Snap and Flatpak versions are prefixed with `v` to match artifact naming expectations and branch naming in Flatpak bundles.
- Confinement:
  - Snap uses `confinement: strict` with GNOME extension on the GUI app.
- Desktop integration:
  - A shared `.desktop` template is rendered differently per platform (`Exec` and `Icon` differ between Snap and Flatpak).
  - Use a 512×512 icon for Flatpak to satisfy export/validation. Snap reuses the same artwork under `meta/gui/`.
  - For Flatpak, extracting with `--strip-components=1` ensures `/app/bin/cryptad` exists; earlier wrapper hacks are no longer needed.

## Operational Notes

- Triggering builds:
  - Use the workflow dispatch UI with either:
    - `version = 1.2.3` (checks out upstream `release/1.2.3`), or
    - `branch = some-branch` (when `version` is empty)
- Artifacts:
  - Snap: two separate artifacts per run (`cryptad-snap-<version>-amd64` and `...-arm64`).
  - Flatpak: two separate artifacts per run (`cryptad-flatpak-<version>-x86_64` and `...-aarch64`).
  - JAR: one file named `cryptad.jar` (no version in filename) included in the drafted upstream release and checksums.
- Upstream dist expectations:
  - Tarball contains `bin/cryptad`, `bin/wrapper-*`, `lib/*`, `conf/wrapper.conf`.
  - The upstream launcher rejects root unless `CRYPTAD_ALLOW_ROOT` is set; the snap sets this env var.
  - Flatpak sets the same env var via `finish-args`.

### Build Manager + Draft Release

- A manager workflow `.github/workflows/release-cryptad-manager.yml` calls all `build-cryptad-*` workflows (snap, flatpak, linux-packages, macos, windows, jar) and then drafts a release in the upstream repo `crypta-network/cryptad`.
- All `build-cryptad-*` workflows remain manually callable (`workflow_dispatch`) and are also reusable (`workflow_call`) so the manager can invoke them.
- The manager accepts the same `version`/`branch` inputs and resolves a final `version` from the child workflows if `version` is empty.
- To publish the draft release in the upstream repository, add a repository secret `UPSTREAM_REPO_TOKEN` with `repo` scope (PAT) that has permission to create releases in `crypta-network/cryptad`.

## Future Enhancements

- Optionally run as a daemon:
  - `apps.cryptad.daemon: simple`
  - Provide `restart-condition` and appropriate plugs/slots as needed.
- Auto‑publish to Snap Store:
  - Add `snapcore/action-publish` and store credentials when ready.
- Enrich version detection if upstream adds explicit Gradle `version` fields.
- Auto‑publish Flatpak to Flathub when ready (add release job and credentials).
- Review/trim `stage-packages` once GUI dependencies are validated across desktops.

## Known Pitfalls (and Avoided Patterns)

- macOS specific: GitHub Actions macOS runners are prone to out‑of‑memory
  errors when executing `gradle test`. For this reason, unit tests are
  intentionally disabled in the macOS DMG workflow (see
  `.github/workflows/build-cryptad-macos.yml`), where `prepare-cryptad`
  is invoked with `run_tests=false` and Gradle is run with `-x test`.
  Rely on Linux/Windows CI for test execution; the macOS job is focused on
  packaging only.
- Don’t use `snapcore/action-build` or LXD cross‑build for this project; it led to wrong JRE arch or network issues.
- Don’t trim platform assets in the workflow; prefer Snapcraft `override-prime` as the single source of truth.
- If Gradle `printVersion` is absent, the workflow falls back to `gradle.properties` → `version:`, then `git describe`.
- Flatpak specific:
  - Ensure Flathub remote is added to the user scope and freedesktop 24.08 runtime+SDK are installed before building.
  - Use a 512×512 icon; smaller icons fail certain `build-export` validations.
  - Use `--strip-components=1` when extracting the payload so `/app/bin/cryptad` exists; otherwise fixups are required.

## Quick References

- Snapcraft docs (core24 platforms):
  https://documentation.ubuntu.com/snapcraft/stable/reference/project-file/snapcraft-yaml/
- Architectures / build plans:
  https://documentation.ubuntu.com/snapcraft/stable/explanation/architectures/
- Flatpak docs:
  https://docs.flatpak.org/en/latest/

## Agent Git Safety Rules

- Never create a new branch or open a pull request without explicit user permission.
- Always ask before running any `git push` to any remote or branch.
- Never set `git user.name` or `git user.email` yourself. If they are missing, remind the user to configure them.
