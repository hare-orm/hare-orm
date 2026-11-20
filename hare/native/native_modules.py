from __future__ import annotations

from rust.native import clock, rows


class NativeModules:
    """The parts of hare's compiled extension ``rust.native`` the Python code works through - built on
    every platform hare installs on."""

    #: ``rust.native.rows`` - attribute operations, relation grouping and value-format helpers.
    rows = rows

    #: ``rust.native.clock`` - the system's precise wall clock.
    clock = clock
