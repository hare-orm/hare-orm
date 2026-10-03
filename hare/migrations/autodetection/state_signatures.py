from __future__ import annotations

import json
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.enums import TriggerForEach, TriggerTiming
from hare.ddl.indexes.index import Index
from hare.ddl.triggers import Trigger
from hare.fields.base.field import Field
from hare.fields.constants import FK_COLUMN_SUFFIX
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.migrations.constants import MODEL_OPTIONS_WITH_OWN_OPERATIONS, RELATION_FIELDS
from hare.migrations.runtime_enums import RuntimeEnums
from hare.query.expressions import Q
from hare.utils.class_path import ClassPath

if TYPE_CHECKING:
    from hare.migrations.state.project.model_state import ModelState

# ---------------------------------------------------------------------------
# Shared normalisation helpers
# ---------------------------------------------------------------------------


class StateSignatures:
    """Comparable signatures of the fields, indexes, constraints and triggers of migration state."""

    @staticmethod
    def _normalize_indexes(value: object) -> list[Index]:
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
    def _get_index_signature(
        index: Index,
    ) -> tuple[tuple[str, ...], str, str, tuple[str, ...], bool, tuple[str, ...], tuple[str, ...]]:
        """A hashable identity of an index without its name - opclasses and uniqueness included."""
        # A Q condition isn't rendered into `extra` - it has no SQL text until a model is at hand.
        condition = getattr(index, "condition", None)
        extra = f"{index.extra}{condition!r}" if isinstance(condition, Q) else index.extra
        orders = tuple(order.value for order in index.get_key_orders())
        if index.fields:
            return (
                tuple(index.field_names),
                index.INDEX_TYPE,
                extra,
                tuple(index.opclasses),
                index.unique,
                tuple(index.include),
                orders,
            )
        return (
            tuple(
                StateSignatures._get_declared_expression_text(expression) for expression in index.declared_expressions
            ),
            index.INDEX_TYPE,
            extra,
            tuple(index.opclasses),
            index.unique,
            tuple(index.include),
            orders,
        )

    @staticmethod
    def _get_named_unique_constraints_by_signature(
        constraints: Iterable[UniqueConstraint | CheckConstraint | ExclusionConstraint],
    ) -> dict[tuple[Any, ...], UniqueConstraint]:
        """The named unique constraints by everything but their name - fields, condition,
        deferral, included columns and NULLs handling - so a constraint only renamed matches
        across two states.

        Args:
            constraints: The normalized constraints of one state.

        Returns:
            Signature -> constraint.
        """
        return {
            (
                tuple(constraint.fields),
                constraint.condition,
                constraint.deferrable,
                constraint.initially_deferred,
                constraint.include,
                constraint.nulls_distinct,
            ): constraint
            for constraint in constraints
            if isinstance(constraint, UniqueConstraint) and constraint.name
        }

    @staticmethod
    def _normalize_constraints(value: object) -> list[UniqueConstraint | CheckConstraint | ExclusionConstraint]:
        if not value or not isinstance(value, Iterable):
            return []
        return [c for c in value if isinstance(c, (UniqueConstraint, CheckConstraint, ExclusionConstraint))]

    @staticmethod
    def _normalize_triggers(value: object) -> list[Trigger]:
        if not value or not isinstance(value, Iterable):
            return []
        return [t for t in value if isinstance(t, Trigger)]

    @staticmethod
    def _get_trigger_signature(
        trigger: Trigger,
    ) -> tuple[str, str, TriggerTiming, TriggerForEach, str | None, str | None, bool, bool]:
        """Return a hashable identity tuple for a Trigger (ignoring its name) - the rename-detection
        equivalent of _get_index_signature() above. Deferral is part of it: a RenameTrigger alone only
        renames, so a changed deferral must not be mistaken for a pure rename."""
        return (
            trigger.on,
            trigger.body,
            trigger.timing,
            trigger.for_each,
            trigger.when,
            trigger.language,
            trigger.deferrable,
            trigger.initially_deferred,
        )

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
            **{name: StateSignatures.get_comparable_value(value) for name, value in kwargs.items()},
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
            or kwargs.get("source_field") == f"{field.model_field_name}{FK_COLUMN_SUFFIX}"
        ):
            signature.pop("source_field", None)
        if isinstance(field, ManyToManyFieldInstance) and field.through_model is not None:
            # A through model indexes its own relations.
            signature.pop("db_index", None)
        if field.has_db_field:
            signature["db_field_types"] = field.get_db_field_types()
        return signature

    @staticmethod
    def get_comparable_value(value: Any) -> object:
        """``value`` as two equal declarations give it, since a value rebuilt from a migration file
        is a new object: a plain value as it is; a collection, value by value; a field by its
        signature; a class by its path; an object a migration file writes as a call
        (``deconstruct()``) by that call; any other object by its text.

        Args:
            value: The value.

        Returns:
            The comparable value - plain values, lists and dicts of them.
        """
        if isinstance(value, (int, float, str, bool, type(None))):
            return value
        if isinstance(value, Field):
            return StateSignatures.get_field_signature(value)
        if isinstance(value, dict):
            return {str(key): StateSignatures.get_comparable_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [StateSignatures.get_comparable_value(item) for item in value]
        if isinstance(value, (set, frozenset)):
            return sorted((StateSignatures.get_comparable_value(item) for item in value), key=repr)
        if isinstance(value, type):
            return ClassPath.get(value)
        deconstruct = getattr(value, "deconstruct", None)
        if callable(deconstruct):
            path, args, kwargs = deconstruct()
            return [path, StateSignatures.get_comparable_value(args), StateSignatures.get_comparable_value(kwargs)]
        return str(value)

    @staticmethod
    def get_field_signature_for_rename(field: Field[Any]) -> dict[str, object]:
        signature = StateSignatures.get_field_signature(field)
        signature.pop("source_field", None)
        return signature

    @staticmethod
    def _get_model_options_for_compare(options: dict[str, object]) -> dict[str, object]:
        # pk_attr/schema are excluded the same way table/indexes/etc. are - each has its own dedicated
        # detection (a pk_attr change raises separately; a schema change generates AlterModelSchema)
        # instead of falling into the generic AlterModelOptions diff, which never moves a table.
        return {key: value for key, value in options.items() if key not in MODEL_OPTIONS_WITH_OWN_OPERATIONS}

    @staticmethod
    def _get_base_signature(bases: Iterable[type]) -> list[str]:
        return [ClassPath.get(base) for base in bases]

    @staticmethod
    def get_model_signature(model_state: ModelState) -> dict[str, object]:
        # A sorted multiset of field content signatures, not keyed by name - a model rename is found
        # even when a field is renamed in the same change.
        fields = sorted(
            json.dumps(StateSignatures.get_field_signature_for_rename(field), sort_keys=True)
            for field in model_state.fields.values()
            if not isinstance(field, RELATION_FIELDS)
        )
        return {
            "fields": fields,
            "options": StateSignatures._get_model_options_for_compare(model_state.options),
            "bases": StateSignatures._get_base_signature(model_state.bases),
            "pk_field_name": model_state.pk_field_name,
            "abstract": model_state.abstract,
        }
