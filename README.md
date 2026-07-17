# common-github
Common Github workflows and actions

**Warning** This repository is public since our public repositories need to access these common actions and workflows. No sensitive information should be committed to this repo.

## Releases

Consumers pin the reusable workflows here to a full commit SHA with the version in a trailing comment:

```yaml
uses: narrative-io/common-github/.github/workflows/backup.yml@<full-40-char-sha> # v1.0.0
```

Tags exist so Dependabot maps each pinned SHA to a version and only bumps consumers when a new version is released (without tags it bumps on every commit to `main`).

Releases are managed by [release-please](https://github.com/googleapis/release-please) from [Conventional Commit](https://www.conventionalcommits.org/) messages:

- `feat:` → minor bump
- `fix:` → patch bump
- `feat!:` / `BREAKING CHANGE:` → major bump
- `docs:`, `chore:`, `ci:`, etc. → no release (docs-only changes never bump consumers)

On merge to `main`, release-please opens or updates a release PR. Merging that PR tags `vX.Y.Z`, publishes a GitHub Release, and updates the changelog. Dependabot then bumps consumers' SHA pins and rewrites the version comment to the new `# vX.Y.Z`. Only immutable `vX.Y.Z` tags are published — there is no floating major (`v1`) tag.
