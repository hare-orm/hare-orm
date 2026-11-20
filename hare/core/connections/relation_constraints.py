from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.core.connections.model_connection_choice import ModelConnectionChoice
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.core.hare_context import HareContext
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models.model_binding import ModelBinding


class RelationConstraints:
    """Database enforcement of a structural relation under explicit execution inputs."""

    @staticmethod
    def is_enforced(
        field: RelationalField[Any],
        context: HareContext | None,
        *,
        connection: DatabaseClient | None = None,
        source_binding: ModelBinding | None = None,
    ) -> bool:
        """Use the referencing node of the retained graph and the compatible runtime client."""
        if not field.db_constraint:
            return False
        source = field.get_binding().model_binding
        if source is None:
            raise ConfigurationError("Relation enforcement requires a prepared model binding")
        binding = source.graph.get_binding(field.get_referencing_model())
        client = connection
        if context is not None and connection is not None and source_binding is not None:
            client = ModelConnectionChoice.get_related_write_client(context, binding, source_binding, connection)
        if client is None:
            if context is None:
                raise ConfigurationError("Relation enforcement requires an application context or explicit client")
            client = ModelConnectionChoice.get_metadata_connection(context, binding, for_write=True)
        return client.enforces_foreign_keys()
