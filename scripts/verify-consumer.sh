#!/usr/bin/env bash
set -euo pipefail

repository=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
consumer=$(mktemp -d)
trap 'rm -rf "$consumer"' EXIT

# The consumer venv has to be built with an interpreter this package actually supports. Left to
# whatever `python3` happens to be on PATH, an older one fails much further downstream: pip filters
# out every release of a dependency that needs a newer Python and reports "could not find a version
# that satisfies jsonschema", which says nothing about the interpreter that caused it.
minimum=$(sed -n 's/^requires-python *= *">=\([0-9.]*\)"/\1/p' "$repository/pyproject.toml")
: "${minimum:?pyproject.toml does not declare requires-python}"

supports_minimum() {
  "$1" -c "import sys; raise SystemExit(0 if sys.version_info >= tuple(
    int(part) for part in '$minimum'.split('.')) else 1)" 2>/dev/null
}

describe() {
  command -v "$1" >/dev/null 2>&1 &&
    "$1" -c 'import platform; print(platform.python_version())' 2>/dev/null ||
    echo "not found"
}

python=""
if [[ -n "${ODP_PYTHON:-}" ]]; then
  # An interpreter named on purpose is used or refused, never quietly swapped for another one.
  if ! supports_minimum "$ODP_PYTHON"; then
    echo "ODP_PYTHON=$ODP_PYTHON is Python $(describe "$ODP_PYTHON")," \
      "and offering-protocol requires >=$minimum." >&2
    exit 1
  fi
  python="$ODP_PYTHON"
else
  for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
    if command -v "$candidate" >/dev/null 2>&1 && supports_minimum "$candidate"; then
      python="$candidate"
      break
    fi
  done
  if [[ -z "$python" ]] && command -v uv >/dev/null 2>&1; then
    python=$(uv python find ">=$minimum" 2>/dev/null || true)
  fi
  if [[ -z "$python" ]]; then
    echo "offering-protocol requires Python >=$minimum;" \
      "python3 is $(describe python3) and no newer interpreter was found." >&2
    echo "Install one, or set ODP_PYTHON to an interpreter that satisfies it." >&2
    exit 1
  fi
fi

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
request = directory.ResourceSearchRequest(types=["collection"])
assert request.to_dict() == {"types": ["collection"]}
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
