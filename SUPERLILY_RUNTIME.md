# SuperLily Nekro Runtime

This repository is the source-of-truth fork for the Nekro runtime used by
Superlily. It preserves the complete upstream history and keeps production
runtime changes as ordinary Git commits rather than patches applied from a
temporary checkout.

## Repository boundaries

- This repository owns the Nekro application source, sandbox lifecycle,
  provider request behavior, prompt compilation, and the production image.
- The [SuperLily parent repository](https://github.com/F1Justin/superlily)
  owns Lily Core, the Nekro-to-Lily bridge plugin, deployment orchestration,
  and the pinned Runtime commit/image identity.
- Persistent production configuration, plugins, uploads, and operational data
  live outside this source checkout and are never part of this repository.

The production branch starts from upstream tag `v2.3.3`, commit
`a34c32f853c3d530b2372b93f68f8bf2469c5333`. The `upstream` remote points to
`KroMiose/nekro-agent`; the writable `origin` remote points to the SuperLily
runtime repository.

## Production changes

- cache-friendly prompt layout and provider usage accounting;
- reduced built-in practice/history overhead while preserving reply targets;
- strict normalization of the known Lily renderer pseudo-module import;
- sandbox bookkeeping by container ID with fresh-client cleanup; and
- non-recursive uploads root permission initialization;
- exact trigger-to-quote binding through an adjacent Reply Focus block that is
  outside the ordinary history count and character budgets; and
- adapter-captured fallback snapshots for quoted messages missing from the
  local history database.

`AI_VISION_IMAGE_LIMIT` remains runtime configuration. Directly quoted images
have a separate `AI_VISION_REPLY_IMAGE_LIMIT`, so newer unrelated images cannot
displace them without changing the ordinary history-image budget.

## Verification and build

```shell
uv sync --all-extras
uv run poe lint
uv run pytest tests/test_superlily_runtime.py

runtime_commit=$(git rev-parse HEAD)
docker build \
  --build-arg VCS_REF="$runtime_commit" \
  --build-arg BUILD_VERSION=<git-tag> \
  -t superlily/nekro-agent:<runtime-tag> \
  -f dockerfile .
```

The production image's `org.opencontainers.image.revision` label must equal the
deployed Runtime commit. Superlily's Compose override is the deployment entry
point and records the stable image tag.
