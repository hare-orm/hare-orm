from __future__ import annotations

import json
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.classes.class_path import ClassPath
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.ddl.schema_objects.trigger import Trigger
from hare.fields.constants import FOREIGN_KEY_COLUMN_SUFFIX
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.migrations.autodetection.field_comparable_values import FieldComparableValues
from hare.migrations.constants import MODEL_OPTIONS_WITH_DEDICATED_OPERATIONS, RELATION_FIELDS
from hare.migrations.runtime_enums import RuntimeEnums
from hare.query.expressions import Q

if TYPE_CHECKING:
    from hare.migrations.state.model_state import ModelState

# ---------------------------------------------------------------------------
# Shared normalisation helpers
# ---------------------------------------------------------------------------


class StateSignatures:
    """Comparable signatures of the fields, indexes, constraints and triggers of migration state."""

    @staticmethod
    def normalize_indexes(value: object) -> list[Index]:
        """Normalise raw index tuples/Index objects into a flat list of Index instances."""
        if not value or not isinstance(value, Iterable):
            return []
        return [item if isinstance(item, Index) else Index(fields=tuple(item)) for item in value]

    @staticmethod
    def _get_declared_expression_text(expression: object) -> str:
        """The code a migration file writes a declared index key expression as - the same for the
        live model's declaration and for one replayed from a migration file, on every database.

        Args:
            expression: The declared key: a hare expression, an ``F(...).desc()`` ordering, or a
                ``RawSQLTerm``.

        Returns:
            The code.
        """
        # Local import: the writer imports the diffing state modules.
        from hare.migrations.writer.import_manager import ImportManager
        from hare.migrations.writer.migration_writer import MigrationWriter

        return MigrationWriter.render_value(expression, ImportManager())

    @staticmethod
    def get_index_signature(
        index: Index,
    ) -> tuple[
        tuple[str, ...], str, str, tuple[str, ...], bool, tuple[str, ...], tuple[str, ...], tuple[tuple[str, Any], ...]
    ]:
        """A hashable identity of an index without its name - opclasses, uniqueness and the
        arguments of its type (a granularity, a Bloom filter's false positive share) included, of the
        index the database keeps."""
        index = index.get_stored_form()
        # A Q condition isn't rendered into `extra` - it has no SQL text until a model is at hand.
        condition = getattr(index, "condition", None)
        extra = f"{index.extra}{condition!r}" if isinstance(condition, Q) else index.extra
        orders = tuple(order.value for order in index.get_key_orders())
        parameter_names = (
            *index.INTEGER_STORAGE_PARAMETERS,
            *index.TEXT_STORAGE_PARAMETERS,
            *index.FLOAT_STORAGE_PARAMETERS,
        )
        storage_parameters = tuple((name, getattr(index, name, None)) for name in parameter_names)
        keys = (
            tuple(index.field_names)
            if index.fields
            else tuple(
                StateSignatures._get_declared_expression_text(expression) for expression in index.declared_expressions
            )
        )
        return (
            keys,
            index.INDEX_TYPE,
            extra,
            tuple(index.opclasses),
            index.unique,
            tuple(index.include),
            orders,
            storage_parameters,
        )

    @staticmethod
    def normalize_constraints(value: object) -> list[UniqueConstraint | CheckConstraint | ExclusionConstraint]:
        if not value or not isinstance(value, Iterable):
            return []
        return [
            constraint
            for constraint in value
            if isinstance(constraint, (UniqueConstraint, CheckConstraint, ExclusionConstraint))
        ]

    @staticmethod
    def normalize_triggers(value: object) -> list[Trigger]:
        if not value or not isinstance(value, Iterable):
            return []
        return [trigger for trigger in value if isinstance(trigger, Trigger)]

    @staticmethod
    def get_field_signature(field: Field[Any]) -> dict[str, object]:
        """What tells whether a field changed between two model states: the field class and the
        arguments it is rebuilt from - as a migration file records them - and its column types.
        What never reaches the database is left out: ``default``, a relation's ``related_name``,
        and a foreign key's ``source_field`` naming the column it has anyway (``<name>_id``, or one
        per key column of a composite key).

        Args:
            field: The field.

        Returns:
            The signature.
        """
        path, __, kwargs = field.deconstruct()
        kwargs.pop("default", None)
        signature: dict[str, object] = {
            "field_type": path,
            **{name: FieldComparableValues.get(value) for name, value in kwargs.items()},
        }
        enum_type = kwargs.get("enum_type")
        if enum_type is not None:
            # By what the enum holds, not by the class - an enum built while the application runs
            # is a new class after every restart.
            signature["enum_type"] = RuntimeEnums.get_content(enum_type)
        if isinstance(field, ForeignKeyFieldInstance | ManyToManyFieldInstance):
            signature.pop("related_name", None)
        if isinstance(field, ForeignKeyFieldInstance) and (
            len(field.source_fields) > 1
            or (isinstance(field.to_field, tuple) and len(field.to_field) > 1)
            or kwargs.get("source_field") == f"{field.model_field_name}{FOREIGN_KEY_COLUMN_SUFFIX}"
        ):
            signature.pop("source_field", None)
        if isinstance(field, ManyToManyFieldInstance) and field.through_model is not None:
            # A through model indexes its own relations.
            signature.pop("db_index", None)
        if field.has_db_field:
            signature["db_field_types"] = field.get_db_field_types()
        return signature

    @staticmethod
    def get_field_signature_for_rename(field: Field[Any]) -> dict[str, object]:
        signature = StateSignatures.get_field_signature(field)
        signature.pop("source_field", None)
        return signature

    @staticmethod
    def get_model_options_for_compare(options: dict[str, object]) -> dict[str, object]:
        # primary_key_attribute/schema are excluded the same way table/indexes/etc. are - each has its own dedicated
        # detection (a primary_key_attribute change raises separately; a schema change generates AlterModelSchema)
        # instead of falling into the generic AlterModelOptions diff, which never moves a table.
        return {key: value for key, value in options.items() if key not in MODEL_OPTIONS_WITH_DEDICATED_OPERATIONS}

    @staticmethod
    def _get_base_signature(bases: Iterable[type]) -> list[str]:
        return [ClassPath.get(base) for base in bases]

    @staticmethod
    def get_model_signature(model_state: ModelState) -> dict[str, object]:
        """What a model's state is made of, its names left out - what tells a renamed model.

        Args:
            model_state: The model's state.

        Returns:
            Its fields' signatures, options and bases.
        """
        # A sorted multiset of field content signatures, not keyed by name - a model rename is found
        # even when a field is renamed in the same change.
        fields = sorted(
            json.dumps(StateSignatures.get_field_signature_for_rename(field), sort_keys=True)
            for field in model_state.fields.values()
            if not isinstance(field, RELATION_FIELDS)
        )
        return {
            "fields": fields,
            "options": StateSignatures.get_model_options_for_compare(model_state.options),
            "bases": StateSignatures._get_base_signature(model_state.bases),
            "pk_field_name": model_state.pk_field_name,
            "abstract": model_state.abstract,
        }
