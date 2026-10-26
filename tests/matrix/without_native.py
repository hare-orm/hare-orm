"""A pytest plugin hiding hare's rust.native extension, as the pure wheel installs none - loaded before
anything imports hare (``pytest -p tests.matrix.without_native``) by the runs of the ``pure`` environment."""

import sys

from tests.matrix.constants import NATIVE_MODULE

sys.modules[NATIVE_MODULE] = None  # type: ignore[assignment]
