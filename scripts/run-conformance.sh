#!/bin/sh
set -eu

specs_dir=${ODP_SPECS_DIR:-../odp-specs}
output_dir=${ODP_CONFORMANCE_OUTPUT:-.conformance/reports}
implementation_version=$(uv run python -c 'from importlib.metadata import version; print(version("offering-protocol"))')

mkdir -p "$output_dir"

for role in agent service; do
  ruby "$specs_dir/ietf/scripts/run_conformance.rb" \
    --role "$role" \
    --implementation-name odp-python \
    --implementation-version "$implementation_version" \
    --output "$output_dir/$role.json" \
    -- uv run python scripts/conformance_adapter.py
done
