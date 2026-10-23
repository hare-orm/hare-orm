from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from hare.models.write.rules.constants import SAVED_OBJECT_ATTRIBUTE_NAMES
from hare.native.native_modules import NativeModules

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


class WrittenInstances:
    """What every write records on the objects whose rows it wrote: they count as saved, on the connection they
    were written through."""

    #: The compiled ``rust.native.rows`` - sets the attributes of many objects in one call; None where it isn't
    #: built.
    native_rows: ClassVar[Any] = NativeModules.rows

    @classmethod
    def mark_saved(cls, objs: Sequence[Model], connection: DatabaseClient) -> None:
        """Marks the objects saved, on ``connection`` - set past the models' ``__setattr__``, as neither is a
        field.

        Args:
            objs: The objects whose rows were written.
            connection: The connection they were written through.
        """
        connection_alias = connection.connection_alias
        if cls.native_rows is not None and len(objs) > 1:
            cls.native_rows.set_attribute_values(objs, SAVED_OBJECT_ATTRIBUTE_NAMES, [True, connection_alias])
            return
        for obj in objs:
            object.__setattr__(obj, "_saved_in_db", True)
            object.__setattr__(obj, "_connection_alias", connection_alias)
