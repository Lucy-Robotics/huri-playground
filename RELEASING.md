# Releasing huri-playground

huri-playground has its own versions, independent of huri-core and
huri-modules.

## Version and semver

The version is `version` in `pyproject.toml`. Between releases `dev` carries
the next version with a `.devN` suffix (`0.1.0.dev0` until the first release,
`0.1.0`). The public surface is the command line (`huri-playground`), the
environment variables (README's table) and the keys of
`huri_playground/modules.yaml` and of the questionnaire files; while the major
version is `0`, a minor bump may break it and a patch bump must not. The
Python modules under `huri_playground/` are an example of a client built on
the SDK, not an API.

## Steps

Maintainers of `Lucy-Robotics/HuRI-Playground`:

1. On `dev`, check the `huri-core[client]` range in `pyproject.toml` against
   the huri-core release this version supports, and update the compatibility
   table in README's "Versions and compatibility". CI must be green with
   `HURI_CORE_REF` / `HURI_MODULES_REF` set to those release tags (run the
   workflow by hand with them). When the huri-core range moves to a new minor,
   move the default `HURI_CORE_REF` of `ci.yml` with it.
2. Open a release PR on `dev` that sets `version` (no `.devN`) and summarizes
   the changes; merge it, then merge `dev` into `main` with a PR named
   `Release X.Y.Z`.
3. Tag `main`'s merge commit `X.Y.Z` (no `v` prefix; the first release is
   `0.1.0`), push the tag and publish a GitHub release whose notes are the
   changelog.
