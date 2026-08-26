# Offering Discovery Protocol for Python

[![CI](https://github.com/offering-protocol/odp-python/actions/workflows/ci.yml/badge.svg)](https://github.com/offering-protocol/odp-python/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![PyPI](https://img.shields.io/pypi/v/offering-protocol)](https://pypi.org/project/offering-protocol/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

Official Python software development kit for the
[Offering Discovery Protocol](https://www.offeringprotocol.org/), the open protocol for discovering
Services and navigating their Offerings.

ODP separates Service discovery from catalog discovery. An Agent searches the canonical Directory
for candidate Services, inspects each Service's live ODP document, and then navigates or searches
that Service's Collections and Offerings.

## Installation

```sh
python -m pip install offering-protocol
```

The distribution provides one typed Python package with modules for each integration role:

| Goal                                          | Module                        |
| --------------------------------------------- | ----------------------------- |
| Work with protocol models and validation      | `offering_protocol.core`      |
| Search the canonical Directory                | `offering_protocol.directory` |
| Discover Services and navigate their catalogs | `offering_protocol.agent`     |
| Publish an ODP Service                        | `offering_protocol.service`   |

The dependency direction remains narrow: Core is transport-independent, Directory depends toward
Core, Agent composes Core and Directory, and Service depends toward Core without depending on Agent
behavior.

## Protocol composition

ODP discovers what a Service offers and how an Agent can act on an Offering. A Service Document and
its Actions can advertise AEP enrollment and MPP or x402 payment requirements, but ODP does not
create credentials, invoke Actions, or submit payments. Applications compose the appropriate
enrollment and payment clients around an Action resolved through ODP.

## Development

Python 3.11 or newer and [uv](https://docs.astral.sh/uv/) are required. Install the locked development
environment and run the complete merge gate with:

```sh
make sync
make verify
```

Format source files with:

```sh
make format
```

The merge gate checks formatting, linting, strict type checking, branch coverage, distribution
metadata, and installation of the built wheel into a clean virtual environment.

See [`odp-specs`](https://github.com/offering-protocol/odp-specs) for the normative draft, schemas,
examples, and test vectors.

## Security

See [SECURITY.md](./SECURITY.md) for vulnerability reporting.

## Releases

Maintainers run the `Release` workflow from `main`. It verifies the package and a clean consumer,
publishes through PyPI Trusted Publishing, attests the distributions, and creates the matching tag
and GitHub release.

## License

MIT.
