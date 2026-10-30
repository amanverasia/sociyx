# Releasing

Releases are published to PyPI from GitHub Actions using Trusted Publishing (no API tokens).

## One-time setup

On PyPI, add a pending publisher (Account settings, Publishing):

- PyPI project name: `sociyx`
- Owner: `amanverasia`
- Repository: `sociyx`
- Workflow name: `release.yml`
- Environment name: `pypi`

Optionally create a `pypi` environment in the GitHub repo settings (add required reviewers if you want a manual gate).

## Each release

1. Bump `version` in `pyproject.toml` and merge to `main` with CI green.
2. Create a GitHub Release tagged `vX.Y.Z` (first release: `v0.0.1`) and publish it.
3. The `Release` workflow builds the sdist and wheel and uploads them to PyPI.
