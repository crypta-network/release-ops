# update_releaser

`update_releaser` is an installable Python CLI that promotes existing Cryptad GitHub release artifacts into a published `core-info.json` descriptor on Hyphanet via FCP.

## What it does

- Fetches prebuilt assets from a GitHub release page URL.
- Inserts package artifacts into Hyphanet via FCP and records CHKs and sizes.
- Uploads short and full changelog text as CHKs.
- Generates deterministic `core-info.json`.
- Publishes descriptor to exactly one target per run: `staging` or `production`.
- Verifies published descriptor identity (`version` + `release_page_url`) and CHKs with `nodata` checks by default (with fallback to the last published result URI if the edition URI fetch fails).
- Publishes emergency revocation messages.

## Requirements

- `uv` installed.
- Python `3.14+` (`.python-version` is set to `3.14`).
- Reachable FCP node (default `127.0.0.1:9481`; override via CLI flags or `FCP_HOST`/`FCP_PORT`).

## Setup

Run from inside `update_releaser/`:

```bash
uv sync
uv run update-releaser --help
```

`pyFreenet3` is installed automatically from:
- `https://github.com/hyphanet/pyFreenet` on branch `py3`

## GitHub CLI setup (for draft/untagged releases)

`--github-source gh` uses the `gh` command, so `gh` must be installed and authenticated.

```bash
gh --version
gh auth status
```

If not authenticated yet:

```bash
gh auth login -h github.com -p https -s repo
gh auth status
```

If you prefer token-based auth in CI/non-interactive environments:

```bash
export GH_TOKEN="<github-token-with-release-access>"
gh auth status
```

## Inputs and defaults

- Release input: one positional GitHub release page URL.
  - Example: `https://github.com/crypta-network/cryptad/releases/tag/v1`
- GitHub fetch backend:
  - Default: `--github-source auto` (tries REST API first, then `gh` fallback)
  - Use `--github-source gh` for draft/untagged releases when `gh` is authenticated
- Workdir base:
  - Default: `./dist`
  - Effective per-edition directory: `./dist/<EDITION>/`
- Staging USK:
  - Default file: `staging-usk.txt` in current working directory.
  - File content must be a base URI ending with `/info/`.
  - If missing during publish, the tool auto-generates a new staging key pair via FCP and writes:
    - private base to the configured `--staging-usk-file` path
    - public base to a companion `*.public.txt` file
  - During verify, if `--staging-usk-file` points to a private key and companion `*.public.txt` exists, the public companion is used automatically.
  - Only required when `--publish-to staging`.
- Production USK:
  - Never accepted via CLI argument.
  - Prompted via hidden terminal input only when `--publish-to production`.

## Quickstart

### Promote to staging (default)

```bash
# if staging-usk.txt is missing, a new key pair is generated automatically
uv run update-releaser promote \
  "https://github.com/crypta-network/cryptad/releases/tag/v1"
```

### Promote to production

```bash
# prompts for production USK base via hidden input
uv run update-releaser promote \
  "https://github.com/crypta-network/cryptad/releases/tag/v1" \
  --publish-to production
```

### With explicit overrides

```bash
uv run update-releaser promote \
  "https://github.com/crypta-network/cryptad/releases/tag/v1" \
  --publish-to staging \
  --staging-usk-file ./staging-usk.txt \
  --workdir ./dist
```

### Emergency revoke

```bash
uv run update-releaser revoke \
  --revoke-ssk "SSK@<revocation-key>/revoked" \
  --message "Emergency: pause v1. Investigating installer regressions."
```

## Subcommands

- `fetch-assets`
- `insert-artifacts`
- `upload-changelogs`
- `generate-core-info`
- `publish-descriptor`
- `verify`
- `promote`
- `revoke`

Each subcommand supports `--help`. Most support `--dry-run`.

## Detailed command usage (validated)

The command sets below are validated against current CLI help output
(`uv run update-releaser --help` and each subcommand `--help`) as of
2026-03-01.

### Full manual test command list

Use this flow when you want to run each phase independently and inspect
intermediate outputs in `./dist-full/<edition>/`.

```bash
cd /work/release-ops/update_releaser
uv sync

export RELEASE_URL="https://github.com/crypta-network/cryptad/releases/tag/v2"
export FCP_HOST="127.0.0.1"
export FCP_PORT="9481"

uv run update-releaser --help

uv run update-releaser fetch-assets "$RELEASE_URL" \
  --github-source gh \
  --workdir ./dist-full \
  -v

uv run update-releaser insert-artifacts "$RELEASE_URL" \
  --fcp-host "$FCP_HOST" \
  --fcp-port "$FCP_PORT" \
  --workdir ./dist-full \
  -v

uv run update-releaser upload-changelogs "$RELEASE_URL" \
  --fcp-host "$FCP_HOST" \
  --fcp-port "$FCP_PORT" \
  --workdir ./dist-full \
  -v

uv run update-releaser generate-core-info "$RELEASE_URL" \
  --workdir ./dist-full \
  -v

uv run update-releaser publish-descriptor "$RELEASE_URL" \
  --publish-to staging \
  --staging-usk-file ./staging-usk.txt \
  --fcp-host "$FCP_HOST" \
  --fcp-port "$FCP_PORT" \
  --workdir ./dist-full \
  -v

cat staging-usk.txt
cat staging-usk.public.txt

uv run update-releaser verify "$RELEASE_URL" \
  --publish-to staging \
  --staging-usk-file ./staging-usk.txt \
  --fcp-host "$FCP_HOST" \
  --fcp-port "$FCP_PORT" \
  --workdir ./dist-full \
  --timeout-s 600 \
  -v
```

`verify` will prefer `staging-usk.public.txt` automatically when
`staging-usk.txt` contains a private key.

### One-shot end-to-end test (separate workdir)

Use this when you want one command to run the whole pipeline in a clean
workdir (`./dist-promote/<edition>/`).

```bash
uv run update-releaser promote "$RELEASE_URL" \
  --github-source gh \
  --publish-to staging \
  --staging-usk-file ./staging-usk.promote.txt \
  --fcp-host "$FCP_HOST" \
  --fcp-port "$FCP_PORT" \
  --workdir ./dist-promote \
  --timeout-s 600 \
  -v
```

### Optional revoke test

```bash
uv run update-releaser revoke \
  --fcp-host "$FCP_HOST" \
  --fcp-port "$FCP_PORT" \
  --revoke-ssk "SSK@<revocation-key>/revoked" \
  --message "Emergency test revoke" \
  -v
```

### What each command stage does

1. `fetch-assets`: resolves release metadata and downloads mapped artifacts.
2. `insert-artifacts`: inserts package artifacts into Hyphanet and records CHKs.
3. `upload-changelogs`: uploads short/full changelog content and records CHKs.
4. `generate-core-info`: renders deterministic `core-info.json` from state.
5. `publish-descriptor`: publishes `core-info.json` to staging or production.
6. `verify`: re-fetches the descriptor and checks CHK retrievability.
7. `promote`: runs stages 1-6 in one command.
8. `revoke`: publishes an emergency revocation payload.

## Draft release fetch with gh

```bash
gh auth status
uv run update-releaser fetch-assets \
  "https://github.com/crypta-network/cryptad/releases/tag/untagged-f5282e216f71045f9c75" \
  --github-source gh
```

Optional preflight check:

```bash
gh release view untagged-f5282e216f71045f9c75 \
  --repo crypta-network/cryptad \
  --json tagName,isDraft,url
```

## Output layout

For edition `123`, default output root is `dist/123/`:

- `release.json`
- `assets/`
- `changelog-short.md`
- `changelog-full.md`
- `core-info.json`
- `audit/core-info.123.json`
- `verify.json`
- `state.json`

`state.json` tracks idempotent progress:
- release identity (`owner`, `repo`, `tag`, `edition`)
- downloaded assets (`path`, `size`, `sha256`)
- inserted package CHKs
- changelog CHKs
- generated core-info hash
- published descriptor URI by target
- verification summary

## Notes

- `version` in `core-info.json` is always a string.
- Edition derivation:
  - `v<digits>` becomes `<digits>`
  - non-numeric tags are sanitized into stable path-safe edition strings
- Default verification uses `nodata` checks; add `--deep` to download referenced CHKs.
