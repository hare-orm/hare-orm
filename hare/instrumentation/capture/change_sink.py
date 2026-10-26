from __future__ import annotations

import abc
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.core.apps.apps import Apps
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.instrumentation.capture.captured_change import CapturedChange
    from hare.instrumentation.enums import ChangePayload, RowOperation
    from hare.models import Model


class ChangeSink(abc.ABC):
    """Where the rows of a model's writes go, in the write's own transaction - declared as
    ``Meta.change_capture``. Every ORM write of the model captures its rows - saves, deletes,
    restores, ``update()``, bulk writes, what ``on_delete`` reaches, the rows of a many-to-many
    through model; raw SQL doesn't. ``hare.contrib.outbox.ChangeCapture`` writes them to a
    transactional outbox.
    """

    #: What a change holds of its rows.
    payload: ChangePayload
    #: The operations captured.
    operations: frozenset[RowOperation]

    def link_models(self, apps: Apps) -> None:
        """Finds the models the sink names by reference in the registry of the models it is declared on -
        called as the registry links the relations; nothing by default.

        Args:
            apps: The registry.
        """

    @abc.abstractmethod
    def get_field_names(self, model: type[Model]) -> tuple[str, ...]:
        """The fields a change of the model holds - each a field written in the model's table.

        Args:
            model: The model the sink is declared on.

        Returns:
            The field names.

        Raises:
            ConfigurationError: The declaration doesn't fit the model.
        """

    def get_written_models(self) -> tuple[type[Model], ...]:
        """The models the sink writes the changes to - each has to live on the connection of the
        captured model, so the changes share its transaction.

        Returns:
            The models - none by default.
        """
        return ()

    def check_connection(self, connection: DatabaseClient) -> None:
        """Refuses a connection the sink can't write the changes of a write on - checked before the write runs,
        so a refusal leaves no row written without its changes. Accepts every connection by default.

        Args:
            connection: The connection of the write.
        """

    @abc.abstractmethod
    async def write(self, connection: DatabaseClient, model: type[Model], changes: list[CapturedChange]) -> None:
        """Writes the changes of one write, in its transaction.

        Args:
            connection: The connection the write ran on - its transaction.
            model: The written model.
            changes: The changed rows.
        """
