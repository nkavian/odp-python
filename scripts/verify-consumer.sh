#!/usr/bin/env bash
set -euo pipefail

repository=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
consumer=$(mktemp -d)
trap 'rm -rf "$consumer"' EXIT

python=${ODP_PYTHON:-python3}
"$python" -m venv "$consumer/.venv"
source=${ODP_CONSUMER_SOURCE:-wheel}
if [[ "$source" == "wheel" ]]; then
  requirement=("$repository"/dist/offering_protocol-*.whl)
  "$consumer/.venv/bin/python" -m pip install --disable-pip-version-check "${requirement[@]}"
elif [[ "$source" == "registry" ]]; then
  version=${ODP_PYTHON_VERSION:?ODP_PYTHON_VERSION is required for a registry consumer check}
  requirement=("offering-protocol==$version")
  for attempt in {1..12}; do
    if "$consumer/.venv/bin/python" -m pip install --disable-pip-version-check \
      "${requirement[@]}"; then
      break
    fi
    if [[ "$attempt" == 12 ]]; then
      exit 1
    fi
    sleep 5
  done
else
  echo "ODP_CONSUMER_SOURCE must be wheel or registry." >&2
  exit 1
fi
"$consumer/.venv/bin/python" - <<'PY'
from offering_protocol import __version__
from offering_protocol import agent, core, directory, service

assert __version__
assert agent.__name__ == "offering_protocol.agent"
assert core.__name__ == "offering_protocol.core"
assert directory.__name__ == "offering_protocol.directory"
assert service.__name__ == "offering_protocol.service"
document = core.parse_service_document(
    b'{"description":"Consumer smoke test","http":{"endpoint_base":"/odp"},'
    b'"language":"en","localizations":["en"],"name":"Consumer",'
    b'"odp_version":"1.0","operations":[{"authentication":"not-required",'
    b'"name":"get-offering"},{"authentication":"not-required",'
    b'"name":"list-offerings"}]}'
)
assert document.name == "Consumer"
try:
    core.parse_resource_identity(
        b'{"id":"plant","service":"not a URI","type":"offering"}'
    )
except core.OdpValidationError:
    pass
else:
    raise AssertionError("URI format validation is unavailable")
PY
