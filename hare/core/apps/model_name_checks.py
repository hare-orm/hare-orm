from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.exceptions import ConfigurationError
from hare.models import Model

if TYPE_CHECKING:
    from hare.core.apps.apps import Apps
    from hare.core.apps.declarations import TableIdentity


class ModelNameChecks:
    """Checks table and schema object name collisions across the models registered on each connection."""

    @staticmethod
    def _check_table_name_collisions(apps: Apps) -> None:
        """Refuses two registered models resolving to the same table on one connection.

        Raises:
            ConfigurationError: Two models share a table.
        """
        # Two models defaulting to one table name are rejected here, naming both - per (connection,
        # schema), as schema generation groups them.
        model_by_table: dict[TableIdentity, type[Model]] = {}
        for app in apps.apps.values():
            for model in app.values():
                if model._meta.swapped is not None:
                    continue
                table_key = apps.get_table_identity(model, model._meta.schema, model._meta.db_table)
                colliding_model = model_by_table.get(table_key)
                if colliding_model is not None and colliding_model is not model:
                    raise ConfigurationError(
                        f"Models {colliding_model._meta.full_name!r} and {model._meta.full_name!r} both "
                        f"resolve to table {model._meta.db_table!r} on connection "
                        f"{model._meta.default_connection!r} - give one of them an explicit Meta.table name."
                    )
                model_by_table[table_key] = model

    @staticmethod
    def _check_object_names(apps: Apps) -> None:
        """Refuses different tables declaring an object of the same name on one connection and
        schema, and one model declaring two constraints or indexes of one name. Historical
        model aliases of the same table share its objects.

        Raises:
            ConfigurationError: Such objects are declared.
        """
        # An explicitly named unique or exclusion constraint, index or trigger inherited from an
        # abstract base would give every concrete subclass a database object of the same name -
        # rejected here, naming both models. A check constraint's name is per table and is exempt.
        constraint_by_name: dict[tuple[str | None, str | None, str], type[Model]] = {}
        for app in apps.apps.values():
            for model in app.values():
                if model._meta.swapped is not None:
                    continue
                names = [
                    constraint.name
                    for constraint in model._meta.constraints
                    if isinstance(constraint, (UniqueConstraint, ExclusionConstraint)) and constraint.name
                ]
                names.extend(index.name for index in model._meta.indexes if isinstance(index, Index) and index.name)
                # Constraints and indexes share their names; a trigger and its function may share one.
                repeated_names = sorted(name for name, count in Counter(names).items() if count > 1)
                if repeated_names:
                    raise ConfigurationError(
                        f"Model {model._meta.full_name!r} declares more than one constraint/index named "
                        f"{repeated_names[0]!r} - give each its own name."
                    )
                if model._meta.triggers:
                    # Imported only for a model declaring triggers - importing it here for every
                    # init would load hare.migrations, which imports this module.
                    from hare.ddl.schema_objects.trigger import Trigger

                    names.extend(trigger.name for trigger in model._meta.triggers if isinstance(trigger, Trigger))
                names.extend(
                    schema_object.name
                    for schema_object in (
                        *model._meta.views,
                        *model._meta.materialized_views,
                        *model._meta.dictionaries,
                        *model._meta.sequences,
                        *model._meta.functions,
                    )
                )
                table_key = apps.get_table_identity(model, model._meta.schema, model._meta.db_table)
                for name in names:
                    constraint_key = (model._meta.default_connection, model._meta.schema, name)
                    colliding_model = constraint_by_name.get(constraint_key)
                    if (
                        colliding_model is not None
                        and colliding_model is not model
                        and apps.get_table_identity(
                            colliding_model, colliding_model._meta.schema, colliding_model._meta.db_table
                        )
                        != table_key
                    ):
                        raise ConfigurationError(
                            f"Models {colliding_model._meta.full_name!r} and {model._meta.full_name!r} both "
                            f"declare a constraint/index named {name!r} on connection "
                            f"{model._meta.default_connection!r} - likely inherited unchanged from a shared "
                            "abstract base. Give each subclass its own explicit name."
                        )
                    constraint_by_name[constraint_key] = model
