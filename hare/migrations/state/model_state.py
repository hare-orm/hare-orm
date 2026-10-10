from __future__ import annotations

from collections.abc import Iterable
from copy import copy
from dataclasses import dataclass
from typing import Any, cast

from hare.ddl.indexes.index import Index
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.fields.constants import FOREIGN_KEY_COLUMN_SUFFIX
from hare.fields.field import Field
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.migrations.state.base_entity_state import BaseEntityState
from hare.migrations.state.state_apps import StateApps
from hare.models import Model
from hare.models.enums import ModelOption


@dataclass
class ModelState(BaseEntityState):
    name: str
    app: str
    table: str
    abstract: bool
    description: str
    options: dict[str, Any]
    bases: tuple[type, ...]
    pk_field_name: str | tuple[str, ...]
    fields: dict[str, Field[Any]]

    def clone(self) -> ModelState:
        return self.__class__(
            name=self.name,
            app=self.app,
            table=self.table,
            abstract=self.abstract,
            description=self.description,
            options=dict(self.options),
            bases=self.bases,
            pk_field_name=self.pk_field_name,
            fields={name: copy(field) for name, field in self.fields.items()},
        )

    def get_option_list(self, key: ModelOption) -> list[Any]:
        """Returns a list-valued model option as a fresh list (``options`` stores it as a tuple)."""
        value = self.options.get(key)
        return list(value) if value else []

    def set_option_list(self, key: ModelOption, values: list[Any]) -> None:
        """Stores a list-valued model option as a tuple, or removes it when ``values`` is empty."""
        if values:
            self.options[key] = tuple(values)
        else:
            self.options.pop(key, None)

    def set_field(self, field_name: str, field: Field[Any]) -> None:
        """Stores `field` under `field_name`, bound to that name the way a model class binds it.

        Args:
            field_name: The field's attribute name.
            field: The field definition to store.
        """
        field.model_field_name = field_name
        self.fields[field_name] = field

    @staticmethod
    def get_field_db_column(field_name: str, field: Field[Any]) -> str | None:
        """The DB column a declared field maps to.

        Args:
            field_name: The field's attribute name.
            field: The declared field.

        Returns:
            The column name, or None for a ManyToManyField (it owns no column).
        """
        if isinstance(field, ManyToManyFieldInstance):
            return None
        if isinstance(field, ForeignKeyFieldInstance):
            return field.source_field or f"{field_name}{FOREIGN_KEY_COLUMN_SUFFIX}"
        return field.source_field or field_name

    def sync_field_index(self, field_name: str, old_field: Field[Any], new_field: Field[Any]) -> None:
        """Keeps the implicit per-field Index entry in step with an altered field's own index flag.

        Args:
            field_name: Name of the altered field.
            old_field: The field definition before the change.
            new_field: The field definition after the change.
        """
        was_indexed = old_field.index and not old_field.pk
        is_indexed = new_field.index and not new_field.pk
        if was_indexed == is_indexed or isinstance(new_field, ManyToManyFieldInstance):
            return
        implicit_index = Index(fields=(field_name,))
        indexes = self.get_option_list(ModelOption.INDEXES)
        if is_indexed:
            covered_fields = [tuple(entry.fields if isinstance(entry, Index) else entry) for entry in indexes]
            if (field_name,) not in covered_fields:
                indexes.append(implicit_index)
        else:
            indexes = [entry for entry in indexes if entry != implicit_index]
        self.set_option_list(ModelOption.INDEXES, indexes)

    def render(self, apps: StateApps, *, deepcopy_fields: bool = True) -> type[Model]:
        meta_class = type("Meta", (), self.options)

        if deepcopy_fields:
            attributes: dict[str, Any] = {name: copy(field) for name, field in self.fields.items()}
        else:
            attributes = dict(self.fields)
        attributes["Meta"] = meta_class
        attributes["_no_comments"] = True

        # A composite primary key's fields don't carry primary_key=True - its marker is put back, or
        # the metaclass would add an "id".
        primary_key_attribute = self.options.get(ModelOption.PRIMARY_KEY_ATTRIBUTE)
        if isinstance(primary_key_attribute, tuple):
            attributes["__migration_composite_pk__"] = CompositePrimaryKey(
                *primary_key_attribute, without_overlaps=self.options.get(ModelOption.PK_WITHOUT_OVERLAPS, False)
            )

        model = type(self.name, self.bases, attributes)
        return cast("type[Model]", model)

    @staticmethod
    def implicit_field_indexes(fields: dict[str, Field[Any]], explicit_indexes: Iterable[Any]) -> list[Index]:
        """The single-field indexes ``db_index=True`` implies - the ones ``AddIndex`` records, so the
        live model's state matches the migrations' state. Not for a primary key, nor a field an
        explicit plain single-field index already covers.
        """
        covered: set[tuple[str, ...]] = set()
        for entry in explicit_indexes:
            if isinstance(entry, Index) and (
                type(entry) is not Index or entry.unique or entry.opclasses or entry.INDEX_TYPE
            ):
                continue
            entry_fields = entry.fields if isinstance(entry, Index) else tuple(entry)
            if entry_fields:
                covered.add(tuple(entry_fields))

        # A ManyToManyField's db_index concerns its through table, not a column of this one.
        return [
            Index(fields=(name,))
            for name, field in fields.items()
            if field.index
            and not field.pk
            and not isinstance(field, ManyToManyFieldInstance)
            and (name,) not in covered
        ]

    @staticmethod
    def get_declared_pk_field_name(model: type[Model]) -> str | tuple[str, ...]:
        """The model's primary key as declared field name(s).

        A OneToOneField(primary_key=True) moves ``Meta.primary_key_attribute`` onto its key column's
        shadow field once relations are initialized; the declared relation field is named instead.

        Args:
            model: The model class.

        Returns:
            The primary key field name, or a tuple of names for a composite primary key.
        """
        primary_key_attribute = model._meta.primary_key_attribute
        if isinstance(primary_key_attribute, tuple):
            return primary_key_attribute
        relation_field = getattr(model._meta.fields_map.get(primary_key_attribute), "reference", None)
        if relation_field is not None and relation_field.model_field_name in model._meta.fields_map:
            return relation_field.model_field_name
        return primary_key_attribute

    @classmethod
    def make_from_model(cls, app_label: str, model: type[Model]) -> ModelState:
        """The state of a live model.

        Args:
            app_label: The app the model belongs to.
            model: The model class.

        Returns:
            The state.
        """
        fields = cls.get_state_fields(model)
        declared_pk_field_name = cls.get_declared_pk_field_name(model)
        options = {
            **cls.get_table_options(model),
            **cls.get_schema_object_options(model),
            **cls.get_index_options(model, fields),
            **cls.get_row_options(model, declared_pk_field_name),
        }
        return cls(
            app=app_label,
            name=model.__name__,
            table=model._meta.db_table,
            abstract=model._meta.abstract,
            description=model._meta.table_description,
            options=options,
            pk_field_name=declared_pk_field_name,
            bases=model.__bases__,
            fields=fields,
        )

    @staticmethod
    def get_state_fields(model: type[Model]) -> dict[str, Field[Any]]:
        """The fields a model declares itself - without the reverse sides of relations, generated
        many-to-many fields and the shadow fields of relation key columns.

        Args:
            model: The model class.

        Returns:
            Copies of the fields by name.
        """
        fields: dict[str, Field[Any]] = {}
        for name, field in model._meta.fields_map.items():
            if isinstance(field, BackwardForeignKeyRelation):
                continue
            if isinstance(field, ManyToManyFieldInstance) and field._generated:
                continue
            if getattr(field, "reference", None) is not None:
                continue
            state_field = copy(field)
            if isinstance(state_field, ForeignKeyFieldInstance) and len(state_field.db_column_names) == 1:
                # Relation initialization repoints source_field at the shadow attribute name -
                # the state records the real column so a rendered model maps to it again.
                state_field.source_field = state_field.db_column_names[0]
            fields[name] = state_field
        return fields

    @staticmethod
    def get_set_options(values_by_option: dict[ModelOption, Any]) -> dict[str, Any]:
        """The options that are set.

        Args:
            values_by_option: The value of each option.

        Returns:
            The options whose value isn't empty.
        """
        return {option: value for option, value in values_by_option.items() if value}

    @staticmethod
    def get_table_options(model: type[Model]) -> dict[str, Any]:
        """The options naming a model's table.

        Args:
            model: The model class.

        Returns:
            The options that are set.
        """
        meta = model._meta
        options = ModelState.get_set_options({ModelOption.ABSTRACT: meta.abstract, ModelOption.TABLE: meta.db_table})
        if meta.db_table:
            # Recorded whenever there is a table - RenameModel tells explicit, not explicit, and
            # recorded before the option existed apart.
            options[ModelOption.TABLE_IS_EXPLICIT] = meta.table_is_explicit
        options.update(ModelState.get_set_options({ModelOption.SCHEMA: meta.schema, ModelOption.APP: meta.app}))
        return options

    @staticmethod
    def get_schema_object_options(model: type[Model]) -> dict[str, Any]:
        """The options listing the schema objects a model declares besides its indexes.

        Args:
            model: The model class.

        Returns:
            The options that are set.
        """
        meta = model._meta
        options = ModelState.get_set_options(
            {
                ModelOption.CONSTRAINTS: meta.constraints,
                ModelOption.TRIGGERS: meta.triggers,
                # A view's queryset is kept as its SQL - a migration file writes it so.
                ModelOption.VIEWS: tuple(view.with_sql_query() for view in meta.views),
                ModelOption.MATERIALIZED_VIEWS: tuple(view.with_sql_query() for view in meta.materialized_views),
                ModelOption.DICTIONARIES: meta.dictionaries,
                ModelOption.FUNCTIONS: meta.functions,
                ModelOption.SEQUENCES: meta.sequences,
                ModelOption.POLICIES: meta.policies,
                ModelOption.GRANTS: meta.grants,
            }
        )
        if meta.row_level_security is not None:
            options[ModelOption.ROW_LEVEL_SECURITY] = meta.row_level_security
        return options

    @classmethod
    def get_index_options(cls, model: type[Model], fields: dict[str, Field[Any]]) -> dict[str, Any]:
        """The options listing a model's indexes and the extensions its fields need.

        Args:
            model: The model class.
            fields: The fields of the model's state.

        Returns:
            The options that are set.
        """
        meta = model._meta
        options: dict[str, Any] = {}
        if meta.indexes:
            for index in meta.indexes:
                if isinstance(index, Index) and index.expressions:
                    # State diffing only sees ModelState snapshots, with no model class to resolve
                    # an index's expressions against - resolved here, while the model exists.
                    index.get_expressions(model)
            options[ModelOption.INDEXES] = meta.indexes
        implicit_indexes = cls.implicit_field_indexes(fields, options.get(ModelOption.INDEXES, ()))
        if implicit_indexes:
            options[ModelOption.INDEXES] = (*options.get(ModelOption.INDEXES, ()), *implicit_indexes)
        field_extensions = {extension for field in fields.values() for extension in field.get_required_extensions()}
        extensions = {*meta.extensions, *field_extensions}
        if extensions:
            options[ModelOption.EXTENSIONS] = tuple(sorted(extensions))
        return options

    @staticmethod
    def get_row_options(model: type[Model], declared_pk_field_name: str | tuple[str, ...]) -> dict[str, Any]:
        """The options about a model's key and the way its rows are written.

        Args:
            model: The model class.
            declared_pk_field_name: The primary key as declared field name(s).

        Returns:
            The options that are set.
        """
        meta = model._meta
        options = ModelState.get_set_options(
            {
                ModelOption.PRIMARY_KEY_ATTRIBUTE: declared_pk_field_name,
                ModelOption.TENANT_SCHEMA: bool(meta.tenant_schema),
                ModelOption.PK_WITHOUT_OVERLAPS: bool(meta.pk_without_overlaps),
            }
        )
        if not meta.has_primary_key:
            # Rendered back, a model state without it would get an added "id".
            options[ModelOption.PRIMARY_KEY] = None
        options.update(
            ModelState.get_set_options(
                {
                    ModelOption.TABLE_DESCRIPTION: meta.table_description,
                    ModelOption.TABLE_OPTIONS: meta.table_options,
                    # A model rendered from state (apps.get_model() in RunPython) keeps its tenant,
                    # soft-delete and optimistic lock fields.
                    ModelOption.TENANT_FIELD: meta.tenant_field,
                    ModelOption.SOFT_DELETE_FIELD: meta.soft_delete_field,
                    ModelOption.SOFT_DELETE_HARD_CASCADE: bool(meta.soft_delete_hard_cascade),
                    ModelOption.OPTIMISTIC_LOCK_FIELD: meta.optimistic_lock_field,
                }
            )
        )
        # Only False is recorded - a model whose table the migrations stop managing stays in the
        # migration state with it, so no operation of it touches the table.
        if meta.managed is False:
            options[ModelOption.MANAGED] = False
        if meta.swappable:
            options[ModelOption.SWAPPABLE] = meta.swappable
        return options
