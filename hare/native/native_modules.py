from __future__ import annotations


class NativeModules:
    """The parts of hare's compiled extension ``rust.native`` the Python code speeds itself up with -
    None for a part where the extension isn't built, and the Python code does the same work there.
    """

    #: ``rust.native.rows`` - attribute operations, relation grouping and value-format helpers.
    try:
        from rust.native import rows
    except ImportError:
        rows = None

    #: ``rust.native.clock`` - the system's precise wall clock.
    try:
        from rust.native import clock
    except ImportError:
        clock = None
