from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from hare.classes.deferred_import import DeferredImport
from hare.exceptions import QueryError, ValidationError
from hare.fields.enums import RelationType
from hare.fields.field import Field
from hare.models.instances.instance_initialization import InstanceInitialization
from hare.models.write.instance_values import InstanceValues
from hare.models.write.rules.constants import ADD_METHOD_NAME, THROUGH_DEFAULTS_METHOD_NAME
from hare.models.write.rules.enums import WriteType
from hare.models.write.rules.relation_key_values import RelationKeyValues
from hare.models.write.rules.row_write_rules import RowWriteRules
from hare.query.constants import PRIMARY_KEY_ALIAS
from hare.query.execution.write_statements import BoundWriteValue
from hare.query.queryset.relations.many_to_many.through_values import ThroughValues
from hare.sql.builder.tables.table import Table

if TYPE_CHECKING:
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
    from hare.models.model_binding import ModelBinding
    from hare.models.tenancy.tenancy import Tenancy
    from hare.models.write.rules.tenant_write_rules import TenantWriteRules
    from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation


class ThroughDefaults:
    """The values of a through model's other fields a new link takes: ``through_defaults``, the model's
    defaults and the active tenant."""

    #: ``Tenancy`` - imported at its first read, as its module imports this one.
    tenancy: ClassVar[DeferredImport[type[Tenancy]]] = DeferredImport("hare.models.tenancy.tenancy", "Tenancy")
    #: ``TenantWriteRules`` - imported at its first read, as its module imports this one.
    tenant_write_rules: ClassVar[DeferredImport[type[TenantWriteRules]]] = DeferredImport(
        "hare.models.write.rules.tenant_write_rules", "TenantWriteRules"
    )

    @staticmethod
    def through_defaults_with_active_tenant(
        relation: ManyToManyRelation[Any], through_defaults: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Adds the through model's one tenant to ``through_defaults`` when it has
        ``Meta.tenant_field`` set and the caller didn't give it.

        Args:
            relation: The many-to-many relation of an instance.
            through_defaults: The values the caller gives the through rows' own fields, None for none.

        Raises:
            QueryError: ``through_defaults`` gives a tenant outside the through model's scope, or
                gives none under a scope of several values.
        """
        through_binding = relation.field.get_through_binding()
        if through_binding is None or through_binding.tenant_field is None:
            return through_defaults
        if ThroughDefaults.tenancy.get_scope(through_binding) is None:
            # The through rows take the tenant given, if any - as the instances' own tenants are trusted.
            return through_defaults
        values = dict(through_defaults or {})
        ThroughDefaults.tenant_write_rules.fill_new_values(through_binding, values, "add")
        return values

    @staticmethod
    def given_field_names(through_binding: ModelBinding, given_values: dict[str, Any]) -> set[str]:
        """Include the storage fields of relations supplied in the prepared values."""
        given_names: set[str] = set()
        for name in given_values:
            given_names.add(name)
            field_object = through_binding.fields_map.get(name)
            if field_object is not None and field_object.relation_type in {
                RelationType.FOREIGN_KEY,
                RelationType.ONE_TO_ONE,
            }:
                given_names.update(field_object.source_fields)  # type: ignore[attr-defined]
        return given_names

    @staticmethod
    def get_default_fields(
        relation: ManyToManyRelation[Any],
        given_values: dict[str, Any],
        *,
        collect_defaults: bool = True,
    ) -> list[tuple[str, Field[Any]]]:
        """Validate required through columns and, for insertion, give its Python-default columns."""
        through_binding = relation.field.get_through_binding()
        if through_binding is None:
            return []
        relation_columns = set(relation.field.forward_keys) | set(relation.field.backward_keys)
        provided = ThroughDefaults.given_field_names(through_binding, given_values)
        default_names: list[str] = []
        for field_name, field_object in through_binding.fields_map.items():
            column_name = field_object.source_field or field_name
            if (
                not field_object.has_db_field
                or field_object.generated
                or column_name in relation_columns
                or field_name in provided
            ):
                continue
            has_default = field_object.default is not None or field_object._default_is_coroutine
            needs_default = not (field_object.null or has_default or field_object.has_db_default())
            if needs_default or collect_defaults:
                has_default = (
                    has_default
                    or getattr(field_object, "auto_now", False)
                    or getattr(field_object, "auto_now_add", False)
                )
            if needs_default and not has_default:
                raise ValidationError(
                    f"{through_binding._model.__name__}.{field_name}: a value is required - "
                    "give it in through_defaults"
                )
            if collect_defaults and has_default:
                default_names.append(field_name)
        if not default_names:
            return []
        names = RowWriteRules.get(through_binding, WriteType.INSERT, default_names, ADD_METHOD_NAME).field_names
        return [(field.source_field or name, field) for name in names for field in (through_binding.fields_map[name],)]

    @staticmethod
    async def through_model_default_values(
        relation: ManyToManyRelation[Any],
        default_fields: list[tuple[str, Field[Any]]],
        *,
        writers: Sequence[Callable[[Any, Any], Any]],
    ) -> list[Any]:
        """One through row's DB-ready values for ``ThroughDefaults.get_default_fields()``'s fields,
        computed per row (a callable default such as ``uuid4`` must produce a fresh value for each
        inserted row). Only the requested fields receive defaults, using the same
        assignment helpers as model construction. Explicit values are already bound
        separately and must not be converted or defaulted again.

        Args:
            relation: The many-to-many relation of an instance.
            default_fields: The fields ``get_default_fields()`` gave.
            writers: The prepared column writers, in default_fields order.
        """
        if not default_fields:
            return []
        # Local import: saving imports the query and relation classes during model initialization.
        from hare.models.instances.instance_saving import InstanceSaving

        through_binding: ModelBinding = relation.field.get_through_binding()  # type: ignore[assignment]

        row_instance = InstanceInitialization.allocate(through_binding, relation._context, initialize=False, values={})
        object.__setattr__(row_instance, "_saved_in_db", False)
        default_names = []
        for _column, field in default_fields:
            if getattr(field, "blind_index_source_field_name", None) is not None:
                continue
            reference: ForeignKeyFieldInstance[Any] | None = field.reference  # type: ignore[assignment]
            name = (
                reference.model_field_name
                if reference is not None and len(reference.source_fields) > 1 and callable(reference.default)
                else field.model_field_name
            )
            if name not in default_names:
                default_names.append(name)
                InstanceInitialization.assign_default(row_instance, name, setattr)
        await InstanceSaving.set_async_default_field(row_instance)
        RowWriteRules.get(through_binding, WriteType.INSERT, default_names, ADD_METHOD_NAME).stamp(
            (row_instance,), InstanceValues()
        )
        return [
            write(getattr(row_instance, field.model_field_name), row_instance)
            for (_column, field), write in zip(default_fields, writers, strict=True)
        ]

    @staticmethod
    def prepare(
        relation: ManyToManyRelation[Any],
        through_defaults: dict[str, Any] | None,
        types: TypeRegistry,
        *,
        fills_tenant: bool = True,
    ) -> ThroughValues:
        """Validate and bind explicit through assignments once before an operation writes.

        The same assignments feed new links and restored links. Active tenant values
        join the declaration here; per-row defaults remain deferred until insertion.

        Args:
            relation: The many-to-many relation of an instance.
            through_defaults: The values the caller gives the through rows' own fields, None for none.
            types: The type registry of the through table's connection.
            fills_tenant: Fill the active tenant for added rows. A no-op or clear-only
                set still validates explicit fields without requiring an insert tenant.

        Raises:
            QueryError: ``through_defaults`` names one of the through model's own FK fields making up the
                relation itself, or a field an insert can't set (``RowWriteRules``).
            FieldError: ``through_defaults`` names no field stored in the through model's table.
        """
        if fills_tenant:
            through_defaults = ThroughDefaults.through_defaults_with_active_tenant(relation, through_defaults)
        if not through_defaults:
            return ThroughValues({}, (), (), {})
        through_binding = relation.field.get_through_binding()
        if through_binding is None:
            raise QueryError(
                f"through_defaults isn't supported on {relation.field.model_field_name!r} - it has no "
                "real through model (through=Model) to hold extra fields."
            )
        rules = RowWriteRules.get(
            through_binding,
            WriteType.INSERT,
            through_defaults,
            THROUGH_DEFAULTS_METHOD_NAME,
        )
        if PRIMARY_KEY_ALIAS in through_defaults:
            through_defaults = InstanceInitialization.expand_pk_kwarg(through_binding, through_defaults)
        through_table = Table(relation.field.through, schema=relation.field.through_schema)
        relation_columns = set(relation.field.forward_keys) | set(relation.field.backward_keys)
        columns = []
        values = []
        assignments = {}
        for field_name, value in rules.add_blind_index_values(through_defaults).items():
            field_object = through_binding.fields_map[field_name]
            column_name = field_object.source_field or field_object.model_field_name
            field_column_names = (
                set(field_object.db_column_names)  # type: ignore[attr-defined]
                if field_object.relation_type in {RelationType.FOREIGN_KEY, RelationType.ONE_TO_ONE}
                else {column_name}
            )
            if field_column_names & relation_columns:
                raise QueryError(
                    f"through_defaults cannot set {field_name!r} - it's one of the through model's "
                    "own FK fields making up the relation itself."
                )
            if field_object.relation_type in {RelationType.FOREIGN_KEY, RelationType.ONE_TO_ONE}:
                # A relation is written as its key columns - from an object, None, or a tuple of a composite
                # key's values, as create() takes it.
                key_names = field_object.source_fields  # type: ignore[attr-defined]
                for index, (key_name, key_value) in enumerate(
                    zip(
                        key_names,
                        RelationKeyValues.get_key_values(through_binding, field_name, value),
                        strict=True,
                    )
                ):
                    columns.append(through_table[through_binding.fields_db_projection[key_name]])
                    field = through_binding.fields_map[key_name]
                    database_value = types.get_db_value(field, key_value, None)
                    assignments[field_name, index] = BoundWriteValue(field, key_value, database_value)
                    values.append(database_value)
                continue
            columns.append(through_table[column_name])
            database_value = types.get_db_value(field_object, value, None)
            assignments[field_name, 0] = BoundWriteValue(field_object, value, database_value)
            values.append(database_value)
        return ThroughValues(dict(through_defaults), tuple(columns), tuple(values), assignments)
