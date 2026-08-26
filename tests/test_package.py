from importlib import import_module
from importlib.metadata import version

import pytest

from offering_protocol import __version__


def test_version_matches_distribution() -> None:
    assert __version__ == version("offering-protocol")


@pytest.mark.parametrize(
    "module",
    [
        "offering_protocol.agent",
        "offering_protocol.core",
        "offering_protocol.directory",
        "offering_protocol.service",
    ],
)
def test_role_module_is_importable(module: str) -> None:
    assert import_module(module).__name__ == module
