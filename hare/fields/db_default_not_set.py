from __future__ import annotations

from collections.abc import Callable
from typing import Any


class DbDefaultNotSet:
    """Sentinel indicating db_default was not provided."""

    def __repr__(self) -> str:
        return "NOT_PROVIDED"

    def __bool__(self) -> bool:
        return False

    def __copy__(self) -> DbDefaultNotSet:
        # The one sentinel - a field copied into a migration state compares with it as itself.
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> DbDefaultNotSet:
        return self

    def __reduce__(self) -> tuple[Callable[[], DbDefaultNotSet], tuple[()]]:
        # Unpickled as the one sentinel too.
        return DbDefaultNotSet.get_sentinel, ()

    @staticmethod
    def get_sentinel() -> DbDefaultNotSet:
        """The one sentinel.

        Returns:
            ``DB_DEFAULT_NOT_SET``.
        """
        from hare.fields.constants import DB_DEFAULT_NOT_SET

        return DB_DEFAULT_NOT_SET
