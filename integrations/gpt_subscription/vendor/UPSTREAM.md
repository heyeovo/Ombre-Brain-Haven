# Vendored Sign in with ChatGPT DevKit

Source: https://github.com/openai/sign-in-with-chatgpt-devkit

Pinned commit: `0a36fefeb913055c8c7a1a29b63d82b96b2e841a`, package `@siwc/local` 0.1.0.
The package is an unpublished private workspace. Runtime dependencies are pinned in
the enclosing package-lock.json; the independent verification directory is not a dependency.

License: the included DevKit Noncommercial License v1.0. Keep LICENSE,
THIRD_PARTY_NOTICES.md and docs/dependency-inventory.json with redistributions.
This integration is for the owner's personal, noncommercial use.

Haven modifications (2026-11-15, also marked in changed source files):
- Export state validation and ID-token verification for the trusted import command.
- Add an optional terminal metadata callback with allowlisted per-response usage.
  Existing streamResponse return value and text behavior remain unchanged.

The vendor tests are upstream regression tests, unchanged. Haven tests live in ../test.

Haven modifications (2026-11-16):
- Passively collect allowlisted quota headers and `codex.rate_limits` stream events
  into optional terminal metadata (`quotaEvidence`). No additional network call,
  credential handling, persistence, or UI change. Absence remains unavailable;
  observed values have unverified quota scope until checked against the same
  account's official usage page. SIWC returning these fields is not yet verified.
- New synthetic quota/parser and helper propagation tests live in ../test; upstream
  vendor tests remain unchanged.
