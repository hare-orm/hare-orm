from __future__ import annotations

from typing import ClassVar

from hare.query.statements.write.delete_query import DeleteQuery
from hare.query.statements.write.returning.delete_returning_query import DeleteReturningQuery


class HardDeleteQuery(DeleteQuery):
    """``QuerySet.hard_delete()``: deletes the matched rows for real, even when
    ``Meta.soft_delete_field`` is set - the related rows follow their ``on_delete`` as a
    ``delete()`` of a model without soft delete would."""

    __slots__ = ()

    deletes_permanently: ClassVar[bool] = True


class HardDeleteReturningQuery(DeleteReturningQuery):
    """``hard_delete().returning(...)``: the hard delete, returning each row it deleted."""

    __slots__ = ()

    deletes_permanently: ClassVar[bool] = True
