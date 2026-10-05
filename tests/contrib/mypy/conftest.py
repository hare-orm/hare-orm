"""The mypy plugin tests need mypy - the ``mypy`` extra."""

import pytest

pytest.importorskip("mypy")
