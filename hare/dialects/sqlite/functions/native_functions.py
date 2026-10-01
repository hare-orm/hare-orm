from __future__ import annotations

from typing import Any, ClassVar

from hare.utils.native_modules import NativeModules


class SqliteNativeFunctions:
    """The compiled ``rust.native.sqlite_functions`` module - the SQLite functions and collations
    whose Python versions ran per row, registered in their place when it is built."""

    #: The module - None where it isn't built.
    module: ClassVar[Any] = NativeModules.sqlite_functions
