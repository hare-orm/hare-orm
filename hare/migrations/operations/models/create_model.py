from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.ddl.indexes.index import Index
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.exceptions import ConfigurationError
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.migrations.constants import MODEL_OPTIONS_WITH_DEDICATED_OPERATIONS
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.constants import DIRECT_RELATION_FIELDS
from hare.migrations.operations.declared_uniqueness_checks import DeclaredUniquenessChecks
from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
from hare.migrations.operations.dependencies.relation_dependencies import RelationDependencies
from hare.migrations.operations.enums import DependencyType
from hare.migrations.operations.fields.add_field import AddField
from hare.migrations.operations.fields.alter_field import AlterField
from hare.migrations.operations.fields.field_like import FieldLike
from hare.migrations.operations.fields.remove_field import RemoveField
from hare.migrations.operations.fields.rename_field import RenameField
from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.operations.models.alter_model_options import AlterModelOptions
from hare.migrations.operations.models.alter_model_table import AlterModelTable
from hare.migrations.operations.models.delete_model import DeleteModel
from hare.migrations.operations.models.model_table_operation import ModelTableOperation
from hare.migrations.operations.models.rename_model import RenameModel
from hare.migrations.operations.operation import Operation
from hare.migrations.operations.schema_objects.object_types.constraint_object_type import ConstraintObjectType
from hare.migrations.operations.schema_objects.object_types.index_object_type import IndexObjectType
from hare.migrations.operations.schema_objects.object_types.trigger_object_type import TriggerObjectType
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.models import Model
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class CreateModel(ModelTableOperation, HareOperation):
    """Creates a model and its table."""

    reads_old_state = False
    backward_reads_state_reached = False

    def __init__(
        self,
        name: str,
        fields: list[tuple[str, FieldLike]],
        options: dict[str, Any] | None = None,
        bases: list[str] | None = None,
        state_only: bool = False,
    ) -> None:
        """
        Args:
            name: The model's class name.
            fields: (name, field) pairs of the model's fields.
            options: The model's Meta options.
            bases: The model's base class names.
            state_only: Only records the model in the migration state - its table already exists,
                e.g. for a model moved here from another app.
        """
        self.raise_if_model_name_unusable(name)
        self.options = options
        self.fields = fields
        self.name = name
        self.bases = bases
        self.state_only = state_only
        self._model: type[Model] | None = None

    def get_referenced_model_labels(self, app_label: str) -> frozenset[str] | None:
        """The model, its bases and the models its fields relate to."""
        return self.get_models_labels(app_label, [self.name, *(self.bases or [])], [field for _, field in self.fields])

    def declares_raw_sql(self) -> bool:
        """Whether a field or an option of the model holds raw SQL."""
        return RawSQLTerm.is_in([field for _, field in self.fields]) or RawSQLTerm.is_in(self.options or {})

    def gives_the_state_of(self, folded: CreateModel, operation: Operation, app_label: str) -> bool:
        """Whether a creation folded with a later operation leaves the state this creation and the
        operation leave - an option naming a removed or renamed field otherwise keeps the operation
        apart (an index on the removed field, a constraint on the renamed one).

        Args:
            folded: The folded creation.
            operation: The later operation.
            app_label: The migrations' app.

        Returns:
            True when both states are the same, and neither refuses the operations.
        """
        # Deferred: the operation generator imports the operations.
        from hare.migrations.autodetection.operation_generator import OperationGenerator

        expected_state = State()
        folded_state = State()
        try:
            self.state_forward(app_label, expected_state)
            operation.state_forward(app_label, expected_state)
            folded.state_forward(app_label, folded_state)
        except (ConfigurationError, IncompatibleStateError):
            return False
        return not OperationGenerator(expected_state, folded_state).generate()

    def with_changes(
        self,
        *,
        name: str | None = None,
        fields: list[tuple[str, FieldLike]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> CreateModel:
        """A copy creating the model with other values.

        Args:
            name: The class name, this one's when None.
            fields: The fields, this one's when None.
            options: The options, this one's when None.

        Returns:
            The operation.
        """
        return CreateModel(
            name if name is not None else self.name,
            fields if fields is not None else list(self.fields),
            options if options is not None else (dict(self.options) if self.options is not None else None),
            self.bases,
            self.state_only,
        )

    def reduce(self, other: Operation, app_label: str) -> list[Operation] | bool:
        """A later deletion, rename, options, table or field change of the model folds into its
        creation - a deletion cancels it."""
        if isinstance(other, DeleteModel) and self.is_same_model_name(self.name, other.name):
            if other.state_only == self.state_only:
                return []
            return super().reduce(other, app_label)
        if self.state_only:
            return super().reduce(other, app_label)
        if isinstance(other, RenameModel) and self.is_same_model_name(self.name, other.old_name):
            return [self._get_renamed(other, app_label)]
        if isinstance(other, AlterModelOptions) and self.is_same_model_name(self.name, other.name):
            options = {
                key: value
                for key, value in (self.options or {}).items()
                if key in MODEL_OPTIONS_WITH_DEDICATED_OPERATIONS or key in other.options
            }
            options.update(other.options)
            return [self.with_changes(options=options)]
        if isinstance(other, AlterModelTable) and self.is_same_model_name(self.name, other.name):
            options = dict(self.options or {})
            for option in (ModelOption.TABLE, ModelOption.TABLE_IS_EXPLICIT):
                options.pop(option, None)
                options.pop(str(option), None)
            # As the alteration leaves the state - a table other than the class's is the model's own, which
            # a later rename keeps.
            table_is_explicit = other.table != self.name.lower()
            if table_is_explicit:
                options[str(ModelOption.TABLE)] = other.table
            options[str(ModelOption.TABLE_IS_EXPLICIT)] = table_is_explicit
            return [self.with_changes(options=options)]
        if isinstance(other, (AddField, AlterField, RemoveField, RenameField)) and self.is_same_model_name(
            self.name, other.model_name
        ):
            field_names = [field_name for field_name, _field in self.fields]
            if isinstance(other, AddField) and other.name not in field_names:
                return [self.with_changes(fields=[*self.fields, (other.name, other.field)])]
            if isinstance(other, AlterField) and other.name in field_names:
                return [self._get_created_with_altered_field(other)]
            if self.declares_raw_sql():
                # Its raw SQL may name the removed or renamed column - the folded creation would make
                # the table without it.
                return super().reduce(other, app_label)
            if isinstance(other, RemoveField) and other.name in field_names:
                folded = self.with_changes(fields=[pair for pair in self.fields if pair[0] != other.name])
                if self.gives_the_state_of(folded, other, app_label):
                    return [folded]
            if isinstance(other, RenameField) and other.old_name in field_names and other.new_name not in field_names:
                renamed_fields = other.get_renamed_model_fields(self.fields, app_label)
                if renamed_fields is not None:
                    folded = self.with_changes(fields=renamed_fields)
                    if self.gives_the_state_of(folded, other, app_label):
                        return [folded]
        return super().reduce(other, app_label)

    def _get_created_with_altered_field(self, alteration: AlterField) -> CreateModel:
        """The creation of the model with a field as a later operation alters it.

        Args:
            alteration: The operation altering the field.

        Returns:
            The operation.
        """
        return self.with_changes(
            fields=[
                (field_name, alteration.field if field_name == alteration.name else field)
                for field_name, field in self.fields
            ]
        )

    def _get_renamed(self, rename: RenameModel, app_label: str) -> CreateModel:
        """The creation of the model under the name a later rename gives it - with the table the
        rename leaves and the model's relations to itself.

        Args:
            rename: The rename.
            app_label: The migrations' app.

        Returns:
            The operation.
        """
        options = None if self.options is None else dict(self.options)
        if options is not None and options.get(ModelOption.TABLE) is not None:
            options[ModelOption.TABLE] = rename.get_renamed_table(
                options[ModelOption.TABLE], options.get(ModelOption.TABLE_IS_EXPLICIT)
            )
        return self.with_changes(
            name=rename.new_name,
            fields=[(name, rename.get_field_relating_to_new_name(field, app_label)) for name, field in self.fields],
            options=options,
        )

    def pop_field(self, field_name: str) -> tuple[FieldLike, list[Index]]:
        """Removes a field and the Meta.indexes entries over it, to be added back by a later
        AddField and AddIndex operations.

        Args:
            field_name: The field's name.

        Returns:
            The removed field and the removed indexes.
        """
        field = next(field for name, field in self.fields if name == field_name)
        self.fields = [pair for pair in self.fields if pair[0] != field_name]
        options = dict(self.options or {})
        kept_indexes: list[Any] = []
        removed_indexes: list[Index] = []
        for entry in options.get(ModelOption.INDEXES, ()):
            index = entry if isinstance(entry, Index) else Index(fields=tuple(entry))
            if field_name in (index.fields or ()) or field_name in index.include:
                removed_indexes.append(index)
            else:
                kept_indexes.append(entry)
        if removed_indexes:
            if kept_indexes:
                options[ModelOption.INDEXES] = tuple(kept_indexes)
            else:
                options.pop(ModelOption.INDEXES, None)
            self.options = options
        return field, removed_indexes

    def get_table_model_names(self) -> tuple[str, ...]:
        return (self.name,)

    def get_provided_dependencies(self, app_label: str) -> list[OperationDependency]:
        return [
            OperationDependency.of_model(app_label, self.name, DependencyType.CREATED),
            *(
                OperationDependency.of_field(app_label, self.name, field_name, DependencyType.CREATED)
                for field_name, _field in self.fields
            ),
        ]

    def get_required_dependencies(self, app_label: str, state: State) -> list[OperationDependency]:
        """The models and fields its relations need, its schema and ENUM types, and - unless it only
        records a model moved here - the removal of an old model whose table it takes."""
        options = self.options or {}
        dependencies = [
            dependency
            for _field_name, field in self.fields
            for dependency in RelationDependencies.get_required_for_relation(app_label, self.name, field)
        ]
        schema = options.get(ModelOption.SCHEMA)
        if schema:
            dependencies.append(OperationDependency.of_schema(schema, DependencyType.CREATED))
        dependencies.extend(
            OperationDependency.of_enum_type(enum_type.schema, enum_type.name, DependencyType.CREATED)
            for _field_name, field in self.fields
            if (enum_type := getattr(field, "requires_enum_type", None)) is not None
        )
        if not self.state_only:
            table = options.get(ModelOption.TABLE) or self.name.lower()
            dependencies.extend(
                OperationDependency.of_model(*model_key, DependencyType.REMOVED)
                for model_key, model_state in state.models.items()
                if model_key != (app_label, self.name)
                and (model_state.table or model_state.name.lower()) == table
                and model_state.options.get(ModelOption.SCHEMA) == schema
            )
        return dependencies

    def describe(self) -> str:
        if self.state_only:
            return f"Create model {self.name} (state only, its table already exists)"
        return f"Create model {self.name}"

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        # A relation's shadow key column belongs to the relation - written as the relation alone.
        source_fields = {
            field.source_field or f"{name}_id"
            for name, field in self.fields
            if field is not None and isinstance(field, ForeignKeyFieldInstance)
        }
        path, args, kwargs = super().deconstruct()
        kwargs["fields"] = [
            (name, field) for name, field in self.fields if field is not None and name not in source_fields
        ]
        if self.options:
            kwargs["options"] = AlterModelOptions.get_written_options(self.options)
        else:
            kwargs.pop("options", None)
        if not self.bases:
            kwargs.pop("bases", None)
        return path, args, kwargs

    @property
    def model(self) -> type[Model]:
        """The model class of the operation's fields and options, built on first use."""
        if not self._model:
            meta_class = type("Meta", (), self.options or {})

            attributes: dict[str, Any] = dict(self.fields)
            attributes["Meta"] = meta_class
            attributes["_no_comments"] = True
            attributes["__migration_state__"] = True

            # A composite primary key's fields don't carry primary_key=True - its marker is put
            # back, or the metaclass would add an "id".
            primary_key_attribute = (self.options or {}).get("primary_key_attribute")
            if isinstance(primary_key_attribute, tuple):
                attributes["__migration_composite_pk__"] = CompositePrimaryKey(
                    *primary_key_attribute,
                    without_overlaps=(self.options or {}).get(ModelOption.PK_WITHOUT_OVERLAPS, False),
                )

            self._model = cast("type[Model]", type(self.name, (Model,), attributes))

        return self._model

    def state_forward(self, app_label: str, state: State) -> None:
        model_state = ModelState.make_from_model(app_label, self.model)
        replaces_a_model = (app_label, self.name) in state.models
        state.models[(app_label, self.name)] = model_state
        if not replaces_a_model:
            # Only the new model is rendered, linked to the rendered ones in place.
            state.mark_model_added(app_label, self.name)
            return

        changed_model_keys = {(app_label, self.name)}

        for field in model_state.fields.values():
            if not isinstance(field, DIRECT_RELATION_FIELDS):
                continue

            related_key = StateApps.split_reference(field.model_name)
            if related_key in state.models:
                changed_model_keys.add(related_key)

        # Also find existing models that reference the newly created model.
        # This handles the case where a model with a FK was created before its
        # target (e.g. alphabetical ordering: Alert before Warehouse).
        new_model_key = (app_label, self.name)
        for key, existing_state in state.models.items():
            if key == new_model_key:
                continue
            for field in existing_state.fields.values():
                # A label, a model class or a swappable reference alike.
                if (
                    isinstance(field, DIRECT_RELATION_FIELDS)
                    and StateApps.split_reference(field.model_name) == new_model_key
                ):
                    changed_model_keys.add(key)
                    break

        state.mark_models_changed(changed_model_keys)

    def raise_if_unsupported(
        self, app_label: str, old_state: State, new_state: State, schema_editor: BaseSchemaEditor
    ) -> None:
        if self.state_only:
            return
        model = new_state.apps.get_model(f"{app_label}.{self.name}")
        if model._meta.managed is False:
            return
        table_options = model._meta.get_table_options(schema_editor.client.dialect)
        if table_options is not None:
            table_options.raise_if_unsupported(model, schema_editor.client.features)
        DeclaredUniquenessChecks.raise_if_unsupported(
            schema_editor, model, DeclaredUniquenessChecks.get_unique_field_declarations(model, model._meta.fields_map)
        )
        for index in model._meta.indexes:
            IndexObjectType.raise_if_object_unsupported(schema_editor, model, index)
        for constraint in model._meta.constraints:
            ConstraintObjectType.raise_if_object_unsupported(schema_editor, model, constraint)
        # Created with the table - a database without triggers refused them after it.
        for trigger in model._meta.triggers:
            TriggerObjectType.raise_if_object_unsupported(schema_editor, model, trigger)

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._create_model_table(app_label, new_state, state_editor)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._delete_model_table(app_label, old_state, state_editor)
