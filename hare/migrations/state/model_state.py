from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.core.apps.relation_linking import RelationLinking
from hare.ddl.schema_requirements import SchemaRequirements
from hare.dialects.base.dialect import Dialect
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.fields.constants import FOREIGN_KEY_COLUMN_SUFFIX
from hare.fields.field import Field
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.migrations.constants import MODEL_OPTIONS_OUTSIDE_MIGRATION_STATE, RELATION_FIELDS
from hare.models import Model
from hare.models.class_building.generic_foreign_keys import GenericForeignKeys
from hare.models.enums import ModelOption
from hare.sql.identifiers import Identifiers

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.migrations.operations.fields.field_like import FieldLike
    from hare.models.model_binding import ModelBinding


@dataclass
class ModelState:
    """A model as the migrations know it: its name, table, fields and options - what the state renders a model
    class of."""

    name: str
    app: str
    table: str
    abstract: bool
    description: str
    options: dict[str, Any]
    bases: tuple[type, ...]
    pk_field_name: str | tuple[str, ...]
    fields: dict[str, Field[Any]]

    @property
    def db_table(self) -> str:
        """The recorded physical table name, or the default for a bare declaration."""
        return self.table or self.get_default_table_name(self.name)

    @staticmethod
    def get_default_table_name(model_name: str) -> str:
        return Identifiers.get_within_limit(model_name.lower())

    @staticmethod
    def is_table_explicit(options: Mapping[str, Any]) -> bool:
        explicit = options.get(ModelOption.TABLE_IS_EXPLICIT)
        return bool(options.get(ModelOption.TABLE)) if explicit is None else explicit

    @classmethod
    def record_table_option(cls, options: dict[str, Any], model_name: str, table: str | None) -> str:
        """Store the physical name together with the explicit naming decision."""
        name = cls.get_default_table_name(model_name) if table is None else table
        options[ModelOption.TABLE] = name
        options[ModelOption.TABLE_IS_EXPLICIT] = table is not None
        return name

    def get_required_extensions(self, dialects: Iterable[Dialect]) -> set[str]:
        """Read extension requirements from declarations without rendering historical models.

        The DDL layer collects requirements for both live models and migration state;
        this adapter supplies state options without coupling schema creation to the
        migration autodetector.
        """
        return self.get_declared_extensions(self.fields, self.options, dialects)

    @staticmethod
    def get_declared_extensions(
        fields: Mapping[str, Field[Any]], options: Mapping[str, Any], dialects: Iterable[Dialect]
    ) -> set[str]:
        """Map migration declarations to the schema extension requirements."""
        primary_key_attribute = options.get(ModelOption.PRIMARY_KEY_ATTRIBUTE)
        return SchemaRequirements.get_extensions(
            dialects,
            declared_extensions=options.get(ModelOption.EXTENSIONS, ()),
            fields_by_name=fields,
            indexes=options.get(ModelOption.INDEXES, ()),
            constraints=options.get(ModelOption.CONSTRAINTS, ()),
            primary_key_without_overlaps=(
                primary_key_attribute
                if options.get(ModelOption.PK_WITHOUT_OVERLAPS) and isinstance(primary_key_attribute, tuple)
                else None
            ),
        )

    def clone(self) -> ModelState:
        """A copy of the state - its options and fields copied."""
        return self.__class__(
            name=self.name,
            app=self.app,
            table=self.table,
            abstract=self.abstract,
            description=self.description,
            options=deepcopy(dict(self.options)),
            bases=self.bases,
            pk_field_name=self.pk_field_name,
            fields=self.snapshot_fields(self.fields),
        )

    @staticmethod
    def snapshot_fields(fields: dict[str, Field[Any]]) -> dict[str, Field[Any]]:
        """Publish named declarations without references to rendered model bindings."""
        result = {}
        for name, field in fields.items():
            declaration = field.snapshot()
            declaration.model_field_name = name
            result[name] = declaration
        return result

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

    def render(self) -> type[Model]:
        """Build the model class; the caller registers it in the state's apps.

        Returns:
            The model.
        """
        return self.build_model(self.name, self.bases, self.fields, self.options)

    @staticmethod
    def build_model(
        name: str, bases: tuple[type, ...], fields: Mapping[str, FieldLike], options: dict[str, Any]
    ) -> type[Model]:
        """Build a historical model from declarations without registering it in an app.

        Publication snapshots fields before binding them. Historical markers disable
        source comments and restore composite primary keys from the stored options.
        """
        meta_class = type("Meta", (), options)

        # Model publication snapshots these declarations before binding them.
        attributes: dict[str, Any] = dict(fields)
        attributes["Meta"] = meta_class
        attributes["_no_comments"] = True
        attributes["__migration_state__"] = True

        # A composite primary key's fields don't carry primary_key=True - its marker is put back, or
        # the metaclass would add an "id".
        primary_key_attribute = options.get(ModelOption.PRIMARY_KEY_ATTRIBUTE)
        if isinstance(primary_key_attribute, tuple) and primary_key_attribute:
            attributes["__migration_composite_pk__"] = CompositePrimaryKey(
                *primary_key_attribute, without_overlaps=options.get(ModelOption.PK_WITHOUT_OVERLAPS, False)
            )

        model = type(name, bases, attributes)
        return model

    @staticmethod
    def get_declared_pk_field_name(binding: ModelBinding) -> str | tuple[str, ...]:
        """Preserve the exact logical or explicit-column key declared before relation linking."""
        return binding.declaration.primary_key_attribute

    @classmethod
    def make_from_declaration(cls, app_label: str, model: type[Model]) -> ModelState:
        """Capture a migration declaration without preparing runtime fields or compiling SQL.

        Explicit source queries were already frozen by their declaration objects. Offline
        ModelQuery declarations remain structural until an editor supplies compilation resources.
        """
        declaration = model._declaration
        meta = declaration.meta
        values: dict[str, Any] = {
            option: getattr(meta, option)
            for option in ModelOption
            if option not in MODEL_OPTIONS_OUTSIDE_MIGRATION_STATE and hasattr(meta, option)
        }
        options = cls.get_set_options(values)
        options[ModelOption.APP] = app_label
        options[ModelOption.PRIMARY_KEY_ATTRIBUTE] = declaration.primary_key_attribute
        if not declaration.primary_key_attribute:
            options[ModelOption.PRIMARY_KEY] = None
        if getattr(meta, ModelOption.MANAGED, True) is False:
            options[ModelOption.MANAGED] = False
        else:
            options.pop(ModelOption.MANAGED, None)
        table = getattr(meta, ModelOption.TABLE, "")
        if table:
            options[ModelOption.TABLE_IS_EXPLICIT] = cls.is_table_explicit(values)
        if declaration.table_description:
            options[ModelOption.TABLE_DESCRIPTION] = declaration.table_description
        if declaration.pk_without_overlaps:
            options[ModelOption.PK_WITHOUT_OVERLAPS] = True
        generics = {
            name: field
            for name, field in declaration.generic_foreign_key_fields.items()
            if field.branch_names and not declaration.abstract
        }
        if generics:
            constraints, indexes = GenericForeignKeys.get_schema_entries(
                tuple(options.get(ModelOption.CONSTRAINTS, ())), tuple(options.get(ModelOption.INDEXES, ())), generics
            )
            if constraints:
                options[ModelOption.CONSTRAINTS] = constraints
            if indexes:
                options[ModelOption.INDEXES] = indexes
        return cls(
            name=model.__name__,
            app=app_label,
            table=table,
            abstract=declaration.abstract,
            description=declaration.table_description,
            options=deepcopy(options),
            bases=cls.get_state_bases(model),
            pk_field_name=declaration.primary_key_attribute,
            fields=cls.snapshot_fields(dict(declaration.fields_map)),
        )

    @classmethod
    def make_from_binding(
        cls,
        app_label: str,
        binding: ModelBinding,
        *,
        with_schema_objects: bool = True,
        client: DatabaseClient | None = None,
    ) -> ModelState:
        """The state of a live model.

        Args:
            app_label: The app the model belongs to.
            binding: The model class.
            with_schema_objects: Whether the state lists the objects the model declares beside its indexes -
                a view's query is built into SQL for it, which needs the connections.
            client: The client used when a declaration needs to compile an embedded query.

        Returns:
            The state.
        """
        model = binding._model
        fields = cls.get_state_fields(binding)
        declared_pk_field_name = cls.get_declared_pk_field_name(binding)
        options = {
            **cls.get_table_options(binding),
            **(cls.get_schema_object_options(binding, client) if with_schema_objects else {}),
            **cls.get_index_options(binding),
            **cls.get_row_options(binding, declared_pk_field_name, client),
        }
        return cls(
            app=app_label,
            name=model.__name__,
            table=binding.db_table,
            abstract=binding.abstract,
            description=binding.table_description,
            options=deepcopy(options),
            pk_field_name=declared_pk_field_name,
            bases=cls.get_state_bases(model),
            fields=fields,
        )

    @classmethod
    def get_state_bases(cls, model: type) -> tuple[type, ...]:
        """A model's bases with each abstract model replaced by its own bases, as in Django - its
        fields and options are the state's own already, and a primary key it declares would be
        declared twice.

        Args:
            model: The model class.

        Returns:
            The bases.
        """
        bases: list[type] = []
        for base in model.__bases__:
            is_abstract_model = base is not Model and issubclass(base, Model) and base._declaration.abstract
            for state_base in cls.get_state_bases(base) if is_abstract_model else (base,):
                if state_base not in bases:
                    bases.append(state_base)
        return tuple(bases)

    @staticmethod
    def get_state_fields(binding: ModelBinding) -> dict[str, Field[Any]]:
        """The fields a model declares itself - without the reverse sides of relations, generated
        many-to-many fields and the shadow fields of relation key columns.

        Args:
            binding: The model class.

        Returns:
            Unbound copies by name, preserving effective relation DDL settings
            without carrying the rendered model's target/key bindings.
        """
        fields: dict[str, Field[Any]] = {}
        for name, field in binding.fields_map.items():
            if isinstance(field, BackwardForeignKeyRelation):
                continue
            if isinstance(field, ManyToManyFieldInstance) and field._generated:
                continue
            if getattr(field, "reference", None) is not None:
                continue
            state_field = field.snapshot()
            state_field.model_field_name = name
            if binding._inited and isinstance(field, RELATION_FIELDS) and isinstance(field.model_name, type):
                target = field.get_related_binding()
                state_field.model_name = f"{target.app}.{target._model.__name__}"  # type: ignore[attr-defined]
            if binding._inited and isinstance(field, ManyToManyFieldInstance):
                through = field.get_through_binding()
                if through is not None:
                    state_field.through_model = f"{through.app}.{through._model.__name__}"  # type: ignore[attr-defined]
                    state_field.declared_through = state_field.through_model  # type: ignore[attr-defined]
            if isinstance(field, ForeignKeyFieldInstance):
                relation: ForeignKeyFieldInstance[Any] = state_field  # type: ignore[assignment]
                # Record the effective schema, including M2M delete rules and indexes
                # covered by another declaration, without retaining the model binding.
                relation.on_delete = field.on_delete
                relation.index = field.index
                relation.to_field = field.to_field
                db_column_names = field.db_column_names
                if len(db_column_names) == 1:
                    relation.source_field = db_column_names[0]
            elif isinstance(field, ManyToManyFieldInstance):
                many_to_many: ManyToManyFieldInstance[Any] = state_field  # type: ignore[assignment]
                many_to_many.on_delete = field.on_delete
                if field.through_model is None and (through_name := field.through):
                    # Freeze an automatic table's name as migration DDL; declared
                    # through-model references and composite key prefixes stay intact.
                    many_to_many.declared_through = through_name
                    many_to_many.through = through_name
            fields[name] = state_field
        return fields

    @staticmethod
    def get_set_options(values_by_option: Mapping[str, Any]) -> dict[str, Any]:
        """The options that are set.

        Args:
            values_by_option: The value of each option.

        Returns:
            The options whose value isn't empty.
        """
        return {option: value for option, value in values_by_option.items() if value}

    @staticmethod
    def get_table_options(binding: ModelBinding) -> dict[str, Any]:
        """The options naming a model's table.

        Args:
            binding: The model class.

        Returns:
            The options that are set.
        """
        options = ModelState.get_set_options(
            {ModelOption.ABSTRACT: binding.abstract, ModelOption.TABLE: binding.db_table}
        )
        if binding.db_table:
            # The physical name alone cannot tell RenameModel whether the table follows the class name.
            options[ModelOption.TABLE_IS_EXPLICIT] = binding.table_is_explicit
        options.update(ModelState.get_set_options({ModelOption.SCHEMA: binding.schema, ModelOption.APP: binding.app}))
        return options

    @staticmethod
    def get_schema_object_options(binding: ModelBinding, client: DatabaseClient | None = None) -> dict[str, Any]:
        """The options listing the schema objects a model declares besides its indexes.

        Args:
            binding: The model class.
            client: The client used when a declaration needs to compile an embedded query.

        Returns:
            The options that are set.
        """
        options = ModelState.get_set_options(
            {
                # A one-to-one relation's own uniqueness goes with the relation.
                ModelOption.CONSTRAINTS: tuple(
                    constraint
                    for constraint in binding.constraints
                    if RelationLinking.get_unique_relation_name(binding, constraint) is None
                ),
                ModelOption.TRIGGERS: binding.triggers,
                # A view's queryset is kept as its SQL - a migration file writes it so.
                ModelOption.VIEWS: tuple(view.with_sql_query(client) for view in binding.views),
                ModelOption.MATERIALIZED_VIEWS: tuple(
                    view.with_sql_query(client) for view in binding.materialized_views
                ),
                ModelOption.DICTIONARIES: binding.dictionaries,
                ModelOption.FUNCTIONS: binding.functions,
                ModelOption.SEQUENCES: binding.sequences,
                ModelOption.POLICIES: binding.policies,
                ModelOption.GRANTS: binding.grants,
            }
        )
        if binding.row_level_security is not None:
            options[ModelOption.ROW_LEVEL_SECURITY] = binding.row_level_security
        return options

    @staticmethod
    def get_index_options(binding: ModelBinding) -> dict[str, Any]:
        """The options listing a model's indexes and the extensions it declares (``Meta.extensions``).

        Args:
            binding: The model class.

        Returns:
            The options that are set.
        """
        options: dict[str, Any] = {}
        if binding.indexes:
            options[ModelOption.INDEXES] = binding.indexes
        if binding.extensions:
            options[ModelOption.EXTENSIONS] = tuple(sorted(binding.extensions))
        return options

    @staticmethod
    def get_row_options(
        binding: ModelBinding, declared_pk_field_name: str | tuple[str, ...], client: DatabaseClient | None = None
    ) -> dict[str, Any]:
        """The options about a model's key and the way its rows are written.

        Args:
            binding: The model class.
            declared_pk_field_name: The primary key as declared field name(s).
            client: The client used when a declaration needs to compile an embedded query.

        Returns:
            The options that are set.
        """
        options = ModelState.get_set_options(
            {
                ModelOption.PRIMARY_KEY_ATTRIBUTE: declared_pk_field_name,
                ModelOption.TENANT_SCHEMA: bool(binding.tenant_schema),
                ModelOption.PK_WITHOUT_OVERLAPS: bool(binding.pk_without_overlaps),
            }
        )
        if not binding.has_primary_key:
            # Rendered back, a model state without it would get an added "id".
            options[ModelOption.PRIMARY_KEY] = None
        options.update(
            ModelState.get_set_options(
                {
                    ModelOption.TABLE_DESCRIPTION: binding.table_description,
                    # A queryset the options hold is kept as its SQL - a migration file writes it so.
                    ModelOption.TABLE_OPTIONS: tuple(
                        entry.with_sql_queries(binding, client) for entry in binding.table_options
                    ),
                    # A model rendered from state (apps.get_model() in RunPython) keeps its tenant,
                    # soft-delete and optimistic lock fields.
                    # As declared - a relation's name, not the key column it stands for in the meta: the
                    # state follows the relation field through a rename or a change to a plain field.
                    ModelOption.TENANT_FIELD: getattr(binding.meta, ModelOption.TENANT_FIELD, None),
                    ModelOption.SOFT_DELETE_FIELD: binding.soft_delete_field,
                    ModelOption.SOFT_DELETE_HARD_CASCADE: bool(binding.soft_delete_hard_cascade),
                    ModelOption.OPTIMISTIC_LOCK_FIELD: binding.optimistic_lock_field,
                }
            )
        )
        # Only False is recorded - a model whose table the migrations stop managing stays in the
        # migration state with it, so no operation of it touches the table.
        if binding.managed is False:
            options[ModelOption.MANAGED] = False
        if binding.swappable:
            options[ModelOption.SWAPPABLE] = binding.swappable
        return options
