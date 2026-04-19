# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versioning follows the package version in `pyproject.toml`.

## [Unreleased]

Nothing yet.

## [0.0.1a0] — 2026-04-19

First public alpha. Package version **`0.0.1a0`** (PEP 440); git release tag **`v0.0.1a0`** (`v` + the same string PyPI/build tools use).

### Added

- Deploy & shipping: `Dockerfile`, `Dockerfile.gpu`, `docker-compose.yml`, `.dockerignore`, GitHub Actions **Release** workflow (tag `v*` → wheel/sdist, smoke test, Docker **CPU + GPU** build + GHCR push, GitHub Release artifacts), and operator notes in `docs/ops.md`.

[Unreleased]: https://github.com/AndreaMichielon/spyoncino/compare/v0.0.1a0...HEAD
[0.0.1a0]: https://github.com/AndreaMichielon/spyoncino/releases/tag/v0.0.1a0
