from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet


@dataclass(slots=True)
class InstanceUpdateGuard:
    """Expected column values for one instance saving through a public save hook."""

    instance: Model
    connection_alias: str
    expected_values: dict[str, Any]
    matching_rows: QuerySet[Any, Any]
    active: bool = field(init=False, default=False)

    current: ClassVar[ContextVar[InstanceUpdateGuard | None]] = ContextVar("hare_instance_update_guard", default=None)

    @contextmanager
    def entered(self) -> Iterator[None]:
        self.active = True
        token = self.current.set(self)
        try:
            yield
        finally:
            # A child task can inherit the context and outlive the save hook.
            self.active = False
            self.current.reset(token)

    @classmethod
    def get_for(cls, instance: Model, connection: DatabaseClient) -> InstanceUpdateGuard | None:
        guard = cls.current.get()
        if (
            guard is not None
            and guard.active
            and guard.instance is instance
            and guard.connection_alias == connection.connection_alias
        ):
            return guard
        return None
