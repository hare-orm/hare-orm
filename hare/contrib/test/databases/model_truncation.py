from __future__ import annotations

import typing
from collections.abc import Collection
from contextlib import suppress
from graphlib import CycleError, TopologicalSorter
from typing import TYPE_CHECKING, cast

from hare import Hare
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
    from hare.models import Model


async def truncate_all_models(
    context: HareContext | None = None, *, connections: Collection[DatabaseClient] | None = None
) -> None:
    """Deletes every row of every registered model's table - not of an unmanaged model or a swapped
    one. Each connection's dialect empties its tables (``TableClearing.clear_tables()``), given in
    foreign-key order and schema-qualified.

    Args:
        context: Empty this context's models on its default connection, instead of the current
            context's models each on its own connection.
        connections: Empty only the models of these connections - each on its own connection.

    Raises:
        ConfigurationError: The models aren't loaded.
    """
    apps = context.apps if context is not None else Hare.apps
    if not apps:
        raise ConfigurationError("apps are not loaded")
    connection = context.get_connection() if context is not None and connections is None else None
    connection_aliases = None if connections is None else {client.connection_alias for client in connections}

    models = list(apps.get_models_iterable())

    if not models:
        return

    # Models can belong to different connections (multi-DB setups) - each connection needs its
    # own dialect check and its own TRUNCATE/DELETE statements, run against that connection.
    models_by_connection: dict[DatabaseClient, list[type[Model]]] = {}
    for model in models:
        # A Meta.managed = False model's table isn't hare's - it may be a view, or not exist - and a
        # swapped model has none.
        if model._meta.managed is False or model._meta.swapped is not None:
            continue
        if connection is not None:
            model_connection = connection
        elif context is not None:
            model_connection = context.connections.get(model._meta.default_connection)
        else:
            model_connection = model._meta.connection
        if connection_aliases is not None and model_connection.connection_alias not in connection_aliases:
            continue
        models_by_connection.setdefault(model_connection, []).append(model)

    for db, connection_models in models_by_connection.items():
        editor = db.dialect.schema_editor_class(db)
        clearing = editor.table_clearing_class

        # Auto-created M2M through tables aren't registered models - each is created by the model
        # declaring its relation, so it's collected from that side only. They reference the
        # models, so they go first.
        auto_through_tables: dict[tuple[str | None, str], None] = {}
        for model in connection_models:
            for many_to_many_field_name in sorted(model._meta.many_to_many_fields):
                many_to_many_field = cast(
                    "ManyToManyFieldInstance[typing.Any]", model._meta.fields_map[many_to_many_field_name]
                )
                if (
                    many_to_many_field._generated
                    or many_to_many_field.through_model is not None
                    or not many_to_many_field.through
                ):
                    continue
                auto_through_tables[(many_to_many_field.through_schema, many_to_many_field.through)] = None
        quoted_tables = [
            *(clearing.get_table_target_sql(editor, table, schema) for schema, table in auto_through_tables),
            *(clearing.get_model_target_sql(editor, model) for model in topological_sort_models(connection_models)),
        ]
        await clearing.clear_tables(db, list(dict.fromkeys(quoted_tables)))


def topological_sort_models(models: list[type[Model]]) -> list[type[Model]]:
    """Sorts models so that a model comes before the models it references - the order rows can be
    deleted in. Self references and targets outside the list do not constrain the order.
    Models blocked by a cycle follow the sortable prefix, in their input order; the
    dialect's clearing operation remains responsible for handling their constraints.
    """
    from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance

    model_set = set(models)
    # Build adjacency for delete order: parent -> children that must be deleted first
    # If Event has FK to Tournament, then Tournament depends on Event being deleted first
    deleted_first_by_model: dict[type[Model], set[type[Model]]] = {model_to_sort: set() for model_to_sort in models}
    for model in models:
        for field in model._meta.fields_map.values():
            if isinstance(field, ForeignKeyFieldInstance):
                related = field.related_model
                if related in model_set and related is not model:
                    deleted_first_by_model[related].add(model)
    sorter = TopologicalSorter(deleted_first_by_model)
    with suppress(CycleError):
        sorter.prepare()
    sorted_models: list[type[Model]] = []
    while sorter.is_active():
        ready = sorter.get_ready()
        sorted_models.extend(ready)
        sorter.done(*ready)

    # Append any remaining (circular deps — fallback)
    emitted = set(sorted_models)
    sorted_models.extend(model for model in models if model not in emitted)

    return sorted_models
