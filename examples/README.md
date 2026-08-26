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
