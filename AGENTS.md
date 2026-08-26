# AGENTS.md

## Repository

This repository contains the official Python distribution for ODP. The `offering_protocol` package
has four role modules:

- `core`: transport-independent protocol models and validation.
- `directory`: canonical Directory integration.
- `agent`: Agent-side Service and catalog discovery.
- `service`: framework-neutral Service integration.

The normative protocol is maintained in `offering-protocol/odp-specs`. Check that source before
implementing or changing wire behavior.

## Verification

Run `make verify` before merging. Public APIs must be typed, documented, and backed by tests and
authoritative protocol behavior.

## Conventions

- Support Python 3.11 and newer; continuous integration covers the minimum and current stable
  versions.
- Keep Core independent of asynchronous runtimes and HTTP clients.
- Keep Service integration independent of FastAPI, Flask, Django, and other web frameworks.
- Return typed exceptions rather than logging from library modules.
- Preserve unknown additive protocol members where the normative schemas permit them.
- Keep dependency direction aligned with the module responsibilities above.
- Describe current behavior; do not leave speculative or historical comments.
- Keep public APIs small, idiomatic, and backed by tests.
