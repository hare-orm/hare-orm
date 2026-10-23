from __future__ import annotations

from typing import TYPE_CHECKING

from hare.core.apps.relation_linking import RelationLinking
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.core.apps.apps import Apps


class CrossConnectionChecks:
    """The refusals of what a model can't do with a model on another connection: a relation the database
    enforces, and capturing its changes into it."""

    @staticmethod
    def check_cross_connection_constraints(apps: Apps) -> None:
        """Refuses a database-enforced relation between models on different connections - a
        foreign key constraint can't reference a table in another database.

        Args:
            apps: The registry.

        Raises:
            ConfigurationError: A FK/O2O/M2M with db_constraint=True points at a model whose
                default connection differs.
        """
        for app in apps.apps.values():
            for model in app.values():
                meta = model._meta
                for field_name in (*meta.foreign_key_fields, *meta.one_to_one_fields, *meta.many_to_many_fields):
                    relation_field = meta.fields_map[field_name]
                    is_generated = getattr(relation_field, "_generated", False)
                    if is_generated or not getattr(relation_field, "db_constraint", False):
                        continue
                    related_model, _ = RelationLinking.get_related_model(apps, relation_field.model_name)  # type: ignore[attr-defined]
                    related_connection = related_model._meta.default_connection
                    if (
                        meta.default_connection is None
                        or related_connection is None
                        or related_connection == meta.default_connection
                    ):
                        continue
                    raise ConfigurationError(
                        f'Relation "{meta.full_name}.{field_name}" points at {related_model._meta.full_name!r}, which '
                        f"lives on connection {related_connection!r} while {meta.full_name!r} lives on "
                        f"{meta.default_connection!r} - a database foreign key constraint can't span two "
                        "databases. Declare the field with db_constraint=False."
                    )

    @staticmethod
    def check_change_capture_connections(apps: Apps) -> None:
        """Refuses a model capturing its changes (``Meta.change_capture``) into a model on another
        connection - the changes are written in the transaction of the write. Runs once the models
        are finalised.

        Args:
            apps: The registry.

        Raises:
            ConfigurationError: A captured model's changes go to another connection.
        """
        for app in apps.apps.values():
            for model in app.values():
                meta = model._meta
                needs = meta.change_capture_needs
                for written_model in needs.sink.get_written_models() if needs is not None else ():
                    written_connection = written_model._meta.default_connection
                    if meta.default_connection is None or written_connection in (None, meta.default_connection):
                        continue
                    raise ConfigurationError(
                        f"{meta.full_name!r} captures its changes into {written_model._meta.full_name!r}, which lives "
                        f"on connection {written_connection!r} while {meta.full_name!r} lives on "
                        f"{meta.default_connection!r} - the changes have to be written in the transaction of the "
                        "write, on its connection."
                    )
