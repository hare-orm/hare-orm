from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar, cast

from hare.exceptions import QueryError, ValidationError
from hare.fields.enums import RelationType
from hare.fields.field import Field
from hare.models.instances.instance_initialization import InstanceInitialization
from hare.models.write.instance_values import InstanceValues
from hare.models.write.rules.constants import ADD_METHOD_NAME, THROUGH_DEFAULTS_METHOD_NAME
from hare.models.write.rules.enums import WriteType
from hare.models.write.rules.relation_key_values import RelationKeyValues
from hare.models.write.rules.row_write_rules import RowWriteRules
from hare.sql.builder.tables.table import Table

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
    from hare.models import Model
    from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation
    from hare.sql.terms.field import Field as SqlField


class ThroughDefaults:
    """The values of a through model's other fields a new link takes: ``through_defaults``, the model's
    defaults and the active tenant."""

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
        # Local import: same package-init cycle as ManyToManyScopeChecks.check_instance_tenant().
        from hare.models.tenancy.tenancy import Tenancy
        from hare.models.write.rules.tenant_write_rules import TenantWriteRules

        through_model = relation.field.through_model_class
        if through_model is None or through_model._meta.tenant_field is None:
            return through_defaults
        if Tenancy.get_scope(through_model) is None:
            # The through rows take the tenant given, if any - as the instances' own tenants are trusted.
            return through_defaults
        values = dict(through_defaults or {})
        TenantWriteRules.fill_new_values(through_model, values, "add")
        return values

    @staticmethod
    def given_field_names(through_model: type[Model], through_defaults: dict[str, Any] | None) -> set[str]:
        """The through model's field names ``through_defaults`` gives a value: the names as written, the
        key fields of a relation given by its name and the primary-key fields ``pk`` stands for.

        Args:
            through_model: The through model.
            through_defaults: The values the caller gives the through rows' own fields, None for none.
        """
        given_names: set[str] = set()
        for name in through_defaults or ():
            given_names.add(name)
            if name == "pk":
                primary_key_attribute = through_model._meta.primary_key_attribute
                given_names.update(
                    (primary_key_attribute,) if isinstance(primary_key_attribute, str) else primary_key_attribute
                )
                continue
            field_object = through_model._meta.fields_map.get(name)
            if field_object is not None and field_object.relation_type in {
                RelationType.FOREIGN_KEY,
                RelationType.ONE_TO_ONE,
            }:
                given_names.update(cast("ForeignKeyFieldInstance[Any]", field_object).source_fields)
        return given_names

    @staticmethod
    def raise_if_required_fields_missing(
        relation: ManyToManyRelation[Any], through_defaults: dict[str, Any] | None
    ) -> None:
        """Refuses through rows missing a value of the through model's that nothing gives - not
        nullable, with no default, not generated, not in ``through_defaults`` - as ``create()``
        refuses an object missing it, on every backend.

        Args:
            relation: The many-to-many relation of an instance.
            through_defaults: The values the caller gives the through rows' own fields, None for none.

        Raises:
            ValidationError: A field gets no value.
        """
        through_model = relation.field.through_model_class
        if through_model is None:
            return
        relation_columns = set(relation.field.forward_keys) | set(relation.field.backward_keys)
        provided = ThroughDefaults.given_field_names(through_model, through_defaults)
        for field_name, field_object in through_model._meta.fields_map.items():
            if (
                field_object.has_db_field
                and not field_object.generated
                and not field_object.null
                and (field_object.source_field or field_name) not in relation_columns
                and field_name not in provided
                and field_object.default is None
                and not field_object._default_is_coroutine
                and not field_object.has_db_default()
                and not getattr(field_object, "auto_now", False)
                and not getattr(field_object, "auto_now_add", False)
            ):
                raise ValidationError(
                    f"{through_model.__name__}.{field_name}: a value is required - give it in through_defaults"
                )

    @staticmethod
    def through_model_default_fields(
        relation: ManyToManyRelation[Any], through_defaults: dict[str, Any] | None
    ) -> list[tuple[str, Field[Any]]]:
        """The through model's fields filled from a Python-side default - ``default=``,
        ``auto_now``/``auto_now_add`` - as ``(column name, field)`` pairs: ``add()`` inserts through
        rows with a raw INSERT. A ``db_default`` is left to the database.

        Args:
            relation: The many-to-many relation of an instance.
            through_defaults: The values the caller gives the through rows' own fields, None for none.
        """
        through_model = relation.field.through_model_class
        if through_model is None:
            return []
        relation_columns = set(relation.field.forward_keys) | set(relation.field.backward_keys)
        provided = ThroughDefaults.given_field_names(through_model, through_defaults)
        default_fields: list[tuple[str, Field[Any]]] = []
        for field_name, field_object in through_model._meta.fields_map.items():
            column_name = field_object.source_field or field_name
            if (
                not field_object.has_db_field
                or field_object.generated
                or column_name in relation_columns
                or field_name in provided
            ):
                continue
            if (
                field_object.default is not None
                or field_object._default_is_coroutine
                or getattr(field_object, "auto_now", False)
                or getattr(field_object, "auto_now_add", False)
            ):
                default_fields.append((column_name, field_object))
        return default_fields

    @staticmethod
    async def through_model_default_values(
        relation: ManyToManyRelation[Any], default_fields: list[tuple[str, Field[Any]]], types: TypeRegistry
    ) -> list[Any]:
        """One through row's DB-ready values for ``ThroughDefaults.through_model_default_fields()``'s fields,
        computed per row (a callable default such as ``uuid4`` must produce a fresh value for each
        inserted row) by letting a real, never-saved through-model instance apply its own defaults.

        Args:
            relation: The many-to-many relation of an instance.
            default_fields: The fields ``through_model_default_fields()`` gave.
            types: The type registry of the through table's connection.
        """
        if not default_fields:
            return []
        through_model = relation.field.through_model_class
        assert through_model is not None  # nosec B101
        row_instance = through_model()
        for field_name, awaitable_default in row_instance._await_when_save.items():
            setattr(row_instance, field_name, await awaitable_default())
        RowWriteRules.get(through_model, WriteType.INSERT, None, ADD_METHOD_NAME).stamp(
            (row_instance,), InstanceValues()
        )
        return [
            types.get_db_value(field_object, getattr(row_instance, field_object.model_field_name), row_instance)
            for _, field_object in default_fields
        ]

    @staticmethod
    def through_defaults_columns_and_values(
        relation: ManyToManyRelation[Any], through_defaults: dict[str, Any] | None, types: TypeRegistry
    ) -> tuple[list[SqlField], list[Any]]:
        """Resolves ``through_defaults`` into the through table's own extra column fields and
        their already-DB-converted values, ready to append onto ``.add()``'s own INSERT column
        list/values.

        Args:
            relation: The many-to-many relation of an instance.
            through_defaults: The values the caller gives the through rows' own fields, None for none.
            types: The type registry of the through table's connection.

        Raises:
            QueryError: ``through_defaults`` names one of the through model's own FK fields making up the
                relation itself, or a field an insert can't set (``RowWriteRules``).
            FieldError: ``through_defaults`` names no field stored in the through model's table.
        """
        if not through_defaults:
            return [], []
        through_model = relation.field.through_model_class
        if through_model is None:
            raise QueryError(
                f"through_defaults isn't supported on {relation.field.model_field_name!r} - it has no "
                "real through model (through=Model) to hold extra fields."
            )
        rules = RowWriteRules.get(through_model, WriteType.INSERT, through_defaults, THROUGH_DEFAULTS_METHOD_NAME)
        if "pk" in through_defaults:
            through_defaults = InstanceInitialization.expand_pk_kwarg(through_model, through_defaults)
        through_table = Table(relation.field.through, schema=relation.field.through_schema)
        relation_columns = set(relation.field.forward_keys) | set(relation.field.backward_keys)
        columns = []
        values = []
        for field_name, value in rules.add_blind_index_values(through_defaults).items():
            field_object = through_model._meta.fields_map[field_name]
            column_name = field_object.source_field or field_object.model_field_name
            field_column_names = (
                set(cast("ForeignKeyFieldInstance[Any]", field_object).db_column_names)
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
                key_names = cast("ForeignKeyFieldInstance[Any]", field_object).source_fields
                for key_name, key_value in zip(
                    key_names, RelationKeyValues.get_key_values(through_model, field_name, value), strict=True
                ):
                    columns.append(through_table[through_model._meta.fields_db_projection[key_name]])
                    values.append(types.get_db_value(through_model._meta.fields_map[key_name], key_value, None))
                continue
            columns.append(through_table[column_name])
            values.append(types.get_db_value(field_object, value, None))
        return columns, values
