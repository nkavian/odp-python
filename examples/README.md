# Runnable examples

The examples use the installed `offering-protocol` package and require no web framework. The Agent
explicitly enables the loopback-only local-development network policy for its default origin.

From the repository root, start the sample Service:

```sh
uv run python examples/service.py
```

In another terminal, inspect it and list its Offerings:

```sh
uv run python examples/agent.py
```

The Agent example defaults to `http://127.0.0.1:4103`. Pass another Service origin as its first
argument to inspect any ODP Service:

```sh
uv run python examples/agent.py https://demo.inflowpay.ai
```

`service.py` demonstrates the minimum Service integration: a framework adapter, `StaticCatalog`,
`list-offerings`, and `get-offering`. It also includes a Collection so the Collection operations can
be exercised. It is intentionally an in-memory example; production Services can implement the same
typed `Catalog` protocol over their own data source.

`agent.py` prints the Service document, lists Collections and Offerings only when those operations
are advertised, and fetches full details for the first Offering. It does not invoke an Action.

## Canonical Directory discovery

```sh
uv run python examples/directory.py sandbox weather
```

Use `production` for the production Directory. Omit `weather` to browse. This example requires a
deployment with `/v1/directory/search`. It requests at most five mixed results, displays Service
and Collection names, reports unusable or unknown results, and retrieves Collection details only
after inspecting the owning Service's advertised anonymous support. It does not enroll, pay or
invoke Actions. Unlike the local Service example above, this uses the real Directory.
The server's bounded result list does not promise every matching result is included.
