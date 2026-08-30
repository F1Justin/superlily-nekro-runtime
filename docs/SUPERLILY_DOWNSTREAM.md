# SuperLily downstream policy

This repository is a production downstream fork of
[Nekro Agent](https://github.com/KroMiose/nekro-agent) for
[SuperLily](https://github.com/F1Justin/superlily). It is not a general
replacement distribution.

## Fork point and source of truth

- Upstream baseline: Nekro Agent `v2.3.3`, commit
  `a34c32f853c3d530b2372b93f68f8bf2469c5333`.
- Production branch: `superlily/runtime-v2.3.3`.
- Downstream release tags: `v2.3.3-superlily.N`.
- The exact deployed tag, commit, and image are owned by the parent repository's
  [`deploy/nekro-runtime.lock.yml`](https://github.com/F1Justin/superlily/blob/master/deploy/nekro-runtime.lock.yml).

The lock file is the only current-production identity. README files describe
policy and intentionally do not duplicate that moving version.

## Why this is a fork rather than an overlay

SuperLily changes prompt compilation, provider requests, sandbox lifecycle,
history/image policy, execution feedback, and observability as one reviewed
Runtime. Keeping these changes as ordinary commits preserves attribution,
reviewable diffs, reproducible images, rollback tags, and comparison with the
upstream fork point. Runtime source is therefore not patched into a temporary
checkout during deployment.

## Change classes

Potentially upstreamable improvements include deterministic sandbox cleanup,
provider usage accounting, cache layout, robust reply binding, bounded quoted
image handling, and actionable execution diagnostics. They should be proposed
upstream as small independent changes when they no longer depend on SuperLily
contracts.

SuperLily-specific changes include the Lily Core bridge, Renderer contract,
production identity headers, project-specific prompt ABI, deployment lock, and
world/effect boundaries owned by the parent repository. These are not presented
as general Nekro defaults.

## Upstream synchronization

The fork does not promise to track upstream `main`, preview releases, or Nekro
NXT. Upstream changes are reviewed deliberately against the pinned v2.3.3
baseline. A future rebase or migration requires an explicit compatibility and
production acceptance decision; it is not performed automatically by CI.

## Releases and support

Tags identify reviewed SuperLily Runtime source. They do not publish to the
upstream Docker Hub namespace or PyPI project. Production images are built and
pinned by SuperLily's deployment process unless a separate downstream registry
policy is introduced.

This is a personal production/research fork. Issues and focused pull requests
are welcome, but there is no response-time, compatibility, or third-party
deployment-support commitment.

## License and attribution

The repository retains Nekro Agent's license, copyright notices, history, and
upstream links. The combined downstream work is distributed under the terms in
the root [`LICENSE`](../LICENSE); those terms are not the unmodified Apache 2.0
license and include additional Nekro Agent conditions. SuperLily does not claim
upstream code as its own or offer an alternative license for the combined work.
