from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from copy import copy
from typing import Any, cast

from hare.classes.comparable_values import ComparableValues
from hare.core.apps.relation_linking import RelationLinking
from hare.ddl.schema_objects.enum_type import EnumType
from hare.dialects.dialect_registry import DialectRegistry
from hare.exceptions import ConfigurationError
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.migrations.autodetection.constants import (
    COLUMN_CHANGING_OPERATIONS,
    MANY_TO_MANY_THROUGH_TABLE_SWAP_SUFFIX,
)
from hare.migrations.autodetection.diffs.state_model_diff import StateModelDiff
from hare.migrations.autodetection.operation_sorter import OperationSorter
from hare.migrations.autodetection.rename_questions import RenameQuestions
from hare.migrations.autodetection.required_extensions import RequiredExtensions
from hare.migrations.autodetection.schema_object_operations import SchemaObjectOperations
from hare.migrations.autodetection.state_signatures import StateSignatures
from hare.migrations.operations import (
    AlterEnumType,
    AlterField,
    CreateEnumType,
    CreateExtension,
    CreateModel,
    CreateSchema,
    DeleteModel,
    DropEnumType,
    HareOperation,
    RemoveField,
    RenameField,
    RenameModel,
)
from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
from hare.migrations.operations.enums import DependencyType
from hare.migrations.operations.fields.field_like import FieldLike
from hare.migrations.operations.operation import Operation
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.models.enums import ModelOption

ModelKey = tuple[str, str]


class OperationGenerator:
    """The migration operations leading from one migration state to another."""

    def __init__(
        self,
        old_state: State,
        new_state: State,
        connection_by_app: dict[str, str | None] | None = None,
        rename_questions: RenameQuestions | None = None,
    ) -> None:
        """
        Args:
            old_state: The state the migration files on disk produce.
            new_state: The state of the current models.
            connection_by_app: Each app's default connection alias - a table of one app is the
                same table as another app's only when both use the same connection.
            rename_questions: The answers whether a removed field, model or app's model is renamed
                or moved - none renamed without them.
        """
        self.old_state = old_state
        self.new_state = new_state
        self.rename_questions = rename_questions or RenameQuestions()
        # The diffs compare indexes by their resolved key expressions.
        old_state.build_index_expressions()
        new_state.build_index_expressions()
        self.connection_by_app = connection_by_app or {}
        #: Populated by generate() - (app, model) now in a selected app -> (app, model) it was
        #: moved from, for every model moved between apps with its table kept as it is.
        self.moved_models: dict[ModelKey, ModelKey] = {}
        #: Populated by generate() - collects StateModelDiff.warnings (see StateFieldDiff's own
        #: docstring for what these are) across every model diffed in this one generate() call.
        self.warnings: list[str] = []
        #: Populated by generate() - collects StateModelDiff.data_loss_warnings (see
        #: StateFieldDiff's own docstring for what these are) across every model diffed in this
        #: one generate() call.
        self.data_loss_warnings: list[str] = []

    @staticmethod
    def _filter_keys(keys: Iterable[ModelKey], app_labels: Sequence[str] | None) -> list[ModelKey]:
        if app_labels is None:
            return sorted(keys)
        app_set = set(app_labels)
        return sorted([key for key in keys if key[0] in app_set])

    def _match_renamed_models(self, old_keys: list[ModelKey], new_keys: list[ModelKey]) -> dict[ModelKey, ModelKey]:
        """The removed models renamed to added ones: a removed and an added model of one app declared
        alike - the one such pair, that the rename questions confirm, or of several, one an answer given
        beforehand pairs.

        Args:
            old_keys: The selected apps' models of the old state.
            new_keys: The selected apps' models of the new state.

        Returns:
            New key -> old key of each renamed model.
        """
        renamed: dict[ModelKey, ModelKey] = {}
        # The models of each app sharing a signature - each model's signature made once.
        removed_by_signature: dict[tuple[str, str], list[ModelKey]] = {}
        for key in old_keys:
            if key not in new_keys:
                signature_key = StateSignatures.get_model_signature_key(self.old_state.models[key])
                removed_by_signature.setdefault((key[0], signature_key), []).append(key)
        added_by_signature: dict[tuple[str, str], list[ModelKey]] = {}
        signature_key_by_added_key: dict[ModelKey, str] = {}
        for key in new_keys:
            if key not in old_keys:
                signature_key = StateSignatures.get_model_signature_key(self.new_state.models[key])
                signature_key_by_added_key[key] = signature_key
                added_by_signature.setdefault((key[0], signature_key), []).append(key)
        for new_key in sorted(signature_key_by_added_key):
            group_key = (new_key[0], signature_key_by_added_key[new_key])
            matching_added = added_by_signature.get(group_key, [])
            matching_removed = removed_by_signature.get(group_key, [])
            if new_key not in matching_added or not matching_removed:
                continue
            if len(matching_added) == 1 and len(matching_removed) == 1:
                old_key = matching_removed[0]
                if not self.rename_questions.ask_model_rename(new_key[0], old_key[1], new_key[1]):
                    continue
            else:
                # Several declared alike: nothing is asked - a wrong pairing would put rows under the wrong
                # model - but an answer given beforehand tells which is which.
                given_key = next(
                    (
                        removed_key
                        for removed_key in matching_removed
                        if self.rename_questions.is_given(f"{removed_key[0]}.{removed_key[1]}", new_key[1])
                    ),
                    None,
                )
                if given_key is None:
                    continue
                old_key = given_key
            renamed[new_key] = old_key
            matching_removed.remove(old_key)
            matching_added.remove(new_key)
        return renamed

    def _get_table_identity(self, key: ModelKey, model_state: ModelState) -> tuple[str | None, str | None, str]:
        """The connection, schema and table a model's rows live in.

        Args:
            key: The model's (app, model name) key.
            model_state: The model's state.

        Returns:
            The connection alias, schema and table name.
        """
        return (
            self.connection_by_app.get(key[0]),
            model_state.options.get(ModelOption.SCHEMA),
            model_state.table or model_state.name.lower(),
        )

    def _get_moved_model_source(self, new_key: ModelKey) -> ModelKey | None:
        """The removed model of another app whose table `new_key` takes over, if any.

        Args:
            new_key: A model present only in the new state.

        Returns:
            The removed model's key, or None when no removed model of another app used the table.

        Raises:
            ConfigurationError: The model also changes in the same run, or several removed
                models used the table.
        """
        new_model_state = self.new_state.models[new_key]
        table_identity = self._get_table_identity(new_key, new_model_state)
        source_keys = [
            old_key
            for old_key, old_model_state in self.old_state.models.items()
            if old_key[0] != new_key[0]
            and old_key not in self.new_state.models
            and self._get_table_identity(old_key, old_model_state) == table_identity
        ]
        source_keys = [
            source_key
            for source_key in source_keys
            if self.rename_questions.ask_model_move(f"{source_key[0]}.{source_key[1]}", f"{new_key[0]}.{new_key[1]}")
        ]
        if not source_keys:
            return None
        if len(source_keys) > 1:
            source_names = ", ".join(f"{app}.{name}" for app, name in sorted(source_keys))
            raise ConfigurationError(
                f"Model {new_key[0]}.{new_key[1]} takes over table {table_identity[2]!r}, which several "
                f"removed models used ({source_names}) - move one model per makemigrations run."
            )
        source_key = source_keys[0]
        model_diff = StateModelDiff(self.old_state.models[source_key], new_model_state, self.rename_questions)
        if model_diff.generate_operations():
            raise ConfigurationError(
                f"Model {source_key[0]}.{source_key[1]} is moved to {new_key[0]}.{new_key[1]} with its "
                f"table {table_identity[2]!r} kept, but its fields or options change in the same run - "
                "move it unchanged first (one makemigrations run), then change it in a second run."
            )
        return source_key

    def _get_moved_model_target(self, old_key: ModelKey) -> ModelKey | None:
        """The added model of another app that takes over the table of the removed `old_key`.

        Args:
            old_key: A model present only in the old state.

        Returns:
            The added model's key, or None when no added model of another app uses the table.
        """
        old_table_identity = self._get_table_identity(old_key, self.old_state.models[old_key])
        for new_key, new_model_state in self.new_state.models.items():
            if (
                new_key[0] != old_key[0]
                and new_key not in self.old_state.models
                and self._get_table_identity(new_key, new_model_state) == old_table_identity
                and self._get_moved_model_source(new_key) == old_key
            ):
                return new_key
        return None

    @staticmethod
    def _get_rendered_field(state: State, key: ModelKey, field_name: str) -> Field[Any] | None:
        """A field of a model as rendered (with its relations initialized) in `state`.

        Args:
            state: The state to look in.
            key: The model's (app, model name) key.
            field_name: The field's name.

        Returns:
            The rendered field, or None when the model or field isn't rendered there.
        """
        try:
            model = state.apps.get_model(f"{key[0]}.{key[1]}")
        except KeyError:
            return None
        return model._meta.fields_map.get(field_name)

    @staticmethod
    def _get_through_model_table(state: State, many_to_many_field: ManyToManyFieldInstance[Any]) -> str | None:
        """The table of the through model an M2M field declares in `state`.

        Args:
            state: The state the field belongs to.
            many_to_many_field: An M2M field declared with ``through=SomeModel``.

        Returns:
            The through model's table, or None when the model isn't part of `state`.
        """
        through_key = OperationGenerator._get_through_model_key(many_to_many_field)
        through_model_state = state.models.get(through_key) if through_key is not None else None
        if through_model_state is None:
            return None
        return through_model_state.table or through_model_state.name.lower()

    @staticmethod
    def _get_through_model_key(many_to_many_field: ManyToManyFieldInstance[Any]) -> ModelKey | None:
        """The (app, model name) key of the through model an M2M field declares.

        Args:
            many_to_many_field: An M2M field.

        Returns:
            The through model's key, None for a relation through an automatic table.
        """
        through_reference = SwappableModelReference.get_model_reference(many_to_many_field.through_model)
        if isinstance(through_reference, str):
            return cast("ModelKey", tuple(through_reference.split(".", 1)))
        if through_reference is not None:
            return (through_reference._meta.app or "", through_reference.__name__)
        return None

    def _get_through_table_swap_operations(
        self, new_keys: list[ModelKey], renamed_models: dict[ModelKey, ModelKey]
    ) -> list[HareOperation]:
        """Moves an automatic M2M through table aside when a through model takes over its name.

        The automatic table is renamed first so the through model's CreateModel can create the
        table under that name - the AlterField switching the relation then copies the rows over.

        Args:
            new_keys: The selected apps' models of the new state.
            renamed_models: New key -> old key of every renamed model.

        Returns:
            The renaming operations to run before any CreateModel.
        """
        operations: list[HareOperation] = []
        for new_key in new_keys:
            old_key = renamed_models.get(new_key, new_key)
            old_model_state = self.old_state.models.get(old_key)
            if old_model_state is None:
                continue
            new_model_state = self.new_state.models[new_key]
            for field_name, new_field in new_model_state.fields.items():
                old_field = old_model_state.fields.get(field_name)
                if (
                    not isinstance(old_field, ManyToManyFieldInstance)
                    or not isinstance(new_field, ManyToManyFieldInstance)
                    or old_field.through_model is not None
                    or new_field.through_model is None
                ):
                    continue
                rendered_old_field = self._get_rendered_field(self.old_state, old_key, field_name)
                if not isinstance(rendered_old_field, ManyToManyFieldInstance):
                    continue
                if rendered_old_field.through != self._get_through_model_table(self.new_state, new_field):
                    continue
                swapped_field = copy(old_field)
                swapped_field.through = f"{rendered_old_field.through}{MANY_TO_MANY_THROUGH_TABLE_SWAP_SUFFIX}"
                operations.append(AlterField(model_name=new_model_state.name, name=field_name, field=swapped_field))
        return operations

    def _swap_through_tables_for_automatic_relations(
        self, operations: list[HareOperation], new_keys: list[ModelKey], renamed_models: dict[ModelKey, ModelKey]
    ) -> list[HareOperation]:
        """Points an M2M relation leaving its through model at a temporary automatic table name
        when the automatic table would take over the through model's table name.

        Args:
            operations: The generated operations - their AlterField is rewritten in place.
            new_keys: The selected apps' models of the new state.
            renamed_models: New key -> old key of every renamed model.

        Returns:
            The renaming operations to run once the through model's table is gone.
        """
        model_key_by_name = {key[1]: key for key in new_keys}
        final_operations: list[HareOperation] = []
        for index, operation in enumerate(operations):
            if not isinstance(operation, AlterField) or not isinstance(operation.field, ManyToManyFieldInstance):
                continue
            new_key = model_key_by_name.get(operation.model_name)
            if new_key is None:
                continue
            old_key = renamed_models.get(new_key, new_key)
            old_model_state = self.old_state.models.get(old_key)
            old_field = old_model_state.fields.get(operation.name) if old_model_state is not None else None
            if (
                not isinstance(old_field, ManyToManyFieldInstance)
                or old_field.through_model is None
                or operation.field.through_model is not None
            ):
                continue
            # A live model's field already carries its derived table name; a replayed one only
            # once rendered.
            new_through = operation.field.through
            if not new_through:
                rendered_new_field = self._get_rendered_field(self.new_state, new_key, operation.name)
                if not isinstance(rendered_new_field, ManyToManyFieldInstance):
                    continue
                new_through = rendered_new_field.through
            if new_through != self._get_through_model_table(self.old_state, old_field):
                continue
            swapped_field = copy(operation.field)
            swapped_field.through = f"{new_through}{MANY_TO_MANY_THROUGH_TABLE_SWAP_SUFFIX}"
            operations[index] = AlterField(model_name=operation.model_name, name=operation.name, field=swapped_field)
            # The automatic table takes the through model's table name once that model is gone.
            through_key = self._get_through_model_key(old_field)
            if through_key is not None:
                operation.generation_dependencies = (
                    OperationDependency.of_model(*through_key, DependencyType.REMOVED),
                )
            final_operations.append(operation)
        return final_operations

    @staticmethod
    def _get_relation_target_key(field: Field[Any]) -> ModelKey | None:
        """The (app, model name) key of the model a relation field points at.

        Args:
            field: A FK/O2O/M2M field.

        Returns:
            The target's key, or None when the reference isn't an "app.Model" string or a model.
        """
        reference = SwappableModelReference.get_model_reference(getattr(field, "model_name", None))
        if not reference:
            return None
        if isinstance(reference, str):
            parts = reference.split(".")
            return (parts[0], parts[1]) if len(parts) == 2 else None
        return (getattr(reference._meta, ModelOption.APP, ""), reference.__name__)

    @classmethod
    def _get_backward_relation_names(cls, state: State, key: ModelKey) -> dict[tuple[ModelKey, str], str]:
        """The backward accessors a model's relation fields register on the models they point at.

        Args:
            state: The state the model belongs to.
            key: The model's (app, model name) key.

        Returns:
            (target model key, backward accessor name) -> name of the relation field registering it.
        """
        model_state = state.models[key]
        backward_relation_names: dict[tuple[ModelKey, str], str] = {}
        for field_name, field in model_state.fields.items():
            if not isinstance(field, (ForeignKeyFieldInstance, ManyToManyFieldInstance)):
                continue
            if isinstance(field, ManyToManyFieldInstance) and field._generated:
                continue
            target_key = cls._get_relation_target_key(field)
            # The rule the linking goes by.
            related_name = RelationLinking.get_backward_relation_name(
                field.related_name, model_state.table or model_state.name.lower(), key[0], model_state.name
            )
            if target_key is None or related_name is None:
                continue
            backward_relation_names[(target_key, related_name)] = field_name
        return backward_relation_names

    def _is_referenced_by_other_models(self, key: ModelKey) -> bool:
        """Whether a relation field of another model of the old state points at `key`.

        Args:
            key: A model of the old state.

        Returns:
            True when another model's FK/O2O/M2M (or an M2M's through model) is `key`.
        """
        model_reference = f"{key[0]}.{key[1]}"
        for other_key, model_state in self.old_state.models.items():
            if other_key == key:
                continue
            for field in model_state.fields.values():
                if not isinstance(field, (ForeignKeyFieldInstance, ManyToManyFieldInstance)):
                    continue
                if self._get_relation_target_key(field) == key:
                    return True
                through_reference = (
                    SwappableModelReference.get_model_reference(field.through_model)
                    if isinstance(field, ManyToManyFieldInstance)
                    else None
                )
                if through_reference == model_reference or (
                    through_reference is not None
                    and not isinstance(through_reference, str)
                    and (through_reference._meta.app, through_reference.__name__) == key
                ):
                    return True
        return False

    def _get_backward_relation_conflicts(
        self, deleted_keys: list[ModelKey]
    ) -> tuple[list[ModelKey], list[tuple[ModelKey, str]]]:
        """Finds the deleted models whose relation registers a backward accessor that a model of
        the new state registers again - created before the deletion, the new relation would
        collide with the still-registered old one.

        Args:
            deleted_keys: The deleted models (moved ones excluded).

        Returns:
            The conflicting deleted models nothing else references, to delete before any model
            is created, and (model key, field name) of the conflicting relation fields of the
            other ones, to remove before any model is created.
        """
        claimed_backward_relation_names: set[tuple[ModelKey, str]] = set()
        for new_key in self.new_state.models:
            claimed_backward_relation_names.update(self._get_backward_relation_names(self.new_state, new_key))
        early_deleted_keys: list[ModelKey] = []
        early_removed_fields: list[tuple[ModelKey, str]] = []
        for deleted_key in deleted_keys:
            conflicting_field_names = sorted(
                field_name
                for backward_relation, field_name in self._get_backward_relation_names(
                    self.old_state, deleted_key
                ).items()
                if backward_relation in claimed_backward_relation_names
            )
            if not conflicting_field_names:
                continue
            if self._is_referenced_by_other_models(deleted_key):
                early_removed_fields.extend((deleted_key, field_name) for field_name in conflicting_field_names)
            else:
                early_deleted_keys.append(deleted_key)
        return early_deleted_keys, early_removed_fields

    def _get_unrecognized_model_rename_warnings(
        self, old_keys: list[ModelKey], new_keys: list[ModelKey], renamed_models: dict[ModelKey, ModelKey]
    ) -> list[str]:
        """Warnings for an added model shaped exactly like a removed one of the same app that
        wasn't recognized as its rename (e.g. two identical models renamed at once).

        Args:
            old_keys: The selected apps' models of the old state.
            new_keys: The selected apps' models of the new state.
            renamed_models: New key -> old key of every recognized rename.

        Returns:
            One warning per such added model.
        """
        renamed_old_keys = set(renamed_models.values())
        removed_keys = [key for key in old_keys if key not in new_keys and key not in renamed_old_keys]
        added_keys = [key for key in new_keys if key not in old_keys and key not in renamed_models]
        warnings: list[str] = []
        removed_names_by_signature: dict[tuple[str, str], list[str]] = {}
        for old_key in removed_keys:
            signature_key = StateSignatures.get_model_signature_key(self.old_state.models[old_key])
            removed_names_by_signature.setdefault((old_key[0], signature_key), []).append(old_key[1])
        for new_key in added_keys:
            signature_key = StateSignatures.get_model_signature_key(self.new_state.models[new_key])
            matching_removed_names = sorted(removed_names_by_signature.get((new_key[0], signature_key), []))
            if not matching_removed_names:
                continue
            removed_names = ", ".join(repr(name) for name in matching_removed_names)
            warnings.append(
                f"Model {new_key[1]!r} (app {new_key[0]!r}) has the exact same fields/options as removed "
                f"model(s) {removed_names} - if it's actually a rename, the generated CreateModel/"
                "DeleteModel pair will DROP the old table's data instead of carrying it over. Answer "
                f"the rename question, or run makemigrations with --rename {new_key[0]}.<old>={new_key[1]}."
            )
        return warnings

    @staticmethod
    def get_create_model_operation(model_state: ModelState) -> CreateModel:
        fields: list[tuple[str, FieldLike]] = list(model_state.fields.items())
        return CreateModel(
            name=model_state.name,
            fields=fields,
            options=model_state.options,
            bases=[base.__name__ for base in model_state.bases],
        )

    @staticmethod
    def _sort_by_dependencies(keys: list[ModelKey], state: State) -> tuple[list[ModelKey], list[tuple[ModelKey, str]]]:
        """Sorts deleted models so that the models others point at come first - the caller deletes
        them in the reverse order - and finds the relations of a cycle among them: those are removed by
        a ``RemoveField`` first, as no order of the deletions removes them.

        Args:
            keys: The deleted models.
            state: The state they are deleted from.

        Returns:
            The ordered keys (targets first), and the ``(model_key, field_name)`` of each relation of a
            cycle.
        """
        key_set = set(keys)
        # Adjacency per FIELD, not just per model - breaking a cycle needs to know exactly
        # which field to defer, not just that model A depends on model B (possibly via
        # several fields, only one of which need actually be deferred to break the cycle).
        field_dependencies: dict[ModelKey, dict[str, ModelKey]] = {model_key: {} for model_key in keys}
        for key in keys:
            model_state = state.models[key]
            for field_name, field in model_state.fields.items():
                if not isinstance(field, (ForeignKeyFieldInstance, ManyToManyFieldInstance)):
                    continue
                dep_key = OperationGenerator._get_relation_target_key(field)
                if dep_key is not None and dep_key in key_set and dep_key != key:
                    field_dependencies[key][field_name] = dep_key

        dependencies_by_model: dict[ModelKey, set[ModelKey]] = {
            model_key: set(dependencies_by_field.values())
            for model_key, dependencies_by_field in field_dependencies.items()
        }

        # Kahn's algorithm
        result: list[ModelKey] = []
        in_degree = {
            model_key: len(dependencies_by_field) for model_key, dependencies_by_field in dependencies_by_model.items()
        }
        queue = sorted([model_key for model_key, model_dependencies in in_degree.items() if model_dependencies == 0])
        while queue:
            node = queue.pop(0)
            result.append(node)
            for model_key, model_dependencies in dependencies_by_model.items():
                if node in model_dependencies:
                    model_dependencies.discard(node)
                    in_degree[model_key] -= 1
                    if in_degree[model_key] == 0:
                        queue.append(model_key)
                        queue.sort()

        # What is left is a cycle among `keys` - one node's dependencies are deferred at a time.
        deferred: list[tuple[ModelKey, str]] = []
        remaining = {model_key for model_key in keys if model_key not in result}
        while remaining:
            node = sorted(remaining)[0]
            for field_name, dep_key in sorted(field_dependencies[node].items()):
                if dep_key in dependencies_by_model[node]:
                    deferred.append((node, field_name))
                    dependencies_by_model[node].discard(dep_key)
            result.append(node)
            remaining.discard(node)
            for model_key in remaining:
                dependencies_by_model[model_key].discard(node)

        return result, deferred

    @staticmethod
    def _collect_schemas(state: State, keys: Iterable[ModelKey] | None = None) -> set[str]:
        """Collect all unique schema names used in a state - of the models' tables and of their fields'
        ``ENUM`` types.

        Args:
            state: The state to scan.
            keys: Only these models' schemas - every model of `state` when omitted.
        """
        schemas: set[str] = set()
        for key in state.models if keys is None else keys:
            model_state = state.models[key]
            schema = model_state.options.get(ModelOption.SCHEMA)
            if schema:
                schemas.add(schema)
            for field in model_state.fields.values():
                enum_type = field.requires_enum_type
                if enum_type is not None and enum_type.schema:
                    schemas.add(enum_type.schema)
        return schemas

    @staticmethod
    def _collect_extensions(state: State, keys: Iterable[ModelKey] | None = None) -> set[str]:
        """Collect every extension name a model in `state` needs on any dialect - a migration file
        runs on any database (``RequiredExtensions``).

        Args:
            state: The state to scan.
            keys: Only these models' extensions - every model of `state` when omitted.
        """
        dialects = DialectRegistry.get_dialects()
        extensions: set[str] = set()
        for key in state.models if keys is None else keys:
            extensions |= RequiredExtensions.get_for_model_state(state.models[key], dialects)
        return extensions

    @staticmethod
    def _collect_enum_types(
        state: State, keys: Iterable[ModelKey] | None = None
    ) -> dict[tuple[str | None, str], EnumType]:
        """The database ``ENUM`` types the fields of `state`'s models are of, by schema and name.

        Args:
            state: The state to scan.
            keys: Only these models' types - every model of `state` when omitted.

        Raises:
            ConfigurationError: Two fields name one type with different labels.
        """
        enum_types: dict[tuple[str | None, str], EnumType] = {}
        for key in state.models if keys is None else keys:
            for field in state.models[key].fields.values():
                enum_type = field.requires_enum_type
                if enum_type is None:
                    continue
                known = enum_types.setdefault(enum_type.key, enum_type)
                if known.labels != enum_type.labels:
                    raise ConfigurationError(
                        f"Two fields name the ENUM type {enum_type.name!r} with different labels: "
                        f"{list(known.labels)} and {list(enum_type.labels)}"
                    )
        return enum_types

    @staticmethod
    def _get_enum_type_order(item: tuple[tuple[str | None, str], EnumType]) -> tuple[str, str]:
        """The order the operations of ENUM types are written in - the default schema's first, then by
        schema and name."""
        (schema, name), _enum_type = item
        return schema or "", name

    def generate(self, app_labels: Sequence[str] | None = None) -> list[HareOperation]:
        """The operations taking the old state to the new one.

        Args:
            app_labels: The apps whose models are compared, every app when None.

        Returns:
            The operations, in the order they run.

        Raises:
            ConfigurationError: A model moved to another app forms a relation cycle with models
                deleted in the same run.
        """
        old_keys = self._filter_keys(self.old_state.models.keys(), app_labels)
        new_keys = self._filter_keys(self.new_state.models.keys(), app_labels)
        renamed_models = self._match_renamed_models(old_keys, new_keys)
        renamed_old_keys = set(renamed_models.values())
        self.warnings.extend(self._get_unrecognized_model_rename_warnings(old_keys, new_keys, renamed_models))

        operations, cleanup_operations = self._get_prerequisite_operations(old_keys, new_keys)
        model_diff_operations, field_operations_by_key = self._get_model_diff_operations(new_keys, renamed_models)
        schema_object_operations = self._get_schema_object_operations(
            model_diff_operations, field_operations_by_key, old_keys, new_keys, renamed_models
        )
        # The objects a rename leaves reading the old names go first, under the models' old names.
        operations.extend(schema_object_operations.early_removals)
        operations.extend(
            RenameModel(old_name=old_key[1], new_name=new_key[1])
            for new_key, old_key in sorted(renamed_models.items())
        )
        operations.extend(schema_object_operations.renames)
        operations.extend(self._get_through_table_swap_operations(new_keys, renamed_models))

        deleted_keys = [
            model_key for model_key in old_keys if model_key not in new_keys and model_key not in renamed_old_keys
        ]
        moved_out_keys = {key for key in deleted_keys if self._get_moved_model_target(key) is not None}
        # A deleted model's relation whose backward accessor a new relation registers again has to
        # be gone before that new relation is created - the whole model when nothing else still
        # references it, else only the conflicting relation field.
        early_deleted_keys, early_removed_fields = self._get_backward_relation_conflicts(
            [key for key in deleted_keys if key not in moved_out_keys]
        )
        operations.extend(DeleteModel(name=key[1]) for key in early_deleted_keys)
        operations.extend(RemoveField(model_name=key[1], name=field_name) for key, field_name in early_removed_fields)
        deleted_keys = [key for key in deleted_keys if key not in early_deleted_keys]

        added_keys = [
            model_key for model_key in new_keys if model_key not in old_keys and model_key not in renamed_models
        ]
        operations.extend(self._get_added_model_operations(added_keys))
        operations.extend(model_diff_operations)
        through_table_restore_operations = self._swap_through_tables_for_automatic_relations(
            operations, new_keys, renamed_models
        )
        operations.extend(self._get_deleted_model_operations(deleted_keys, moved_out_keys, early_removed_fields))
        operations.extend(through_table_restore_operations)
        operations.extend(schema_object_operations.late_operations)
        operations.extend(schema_object_operations.last_operations)
        operations.extend(cleanup_operations)
        operations = self._add_removals_of_relations_through_changed_keys(operations, old_keys, renamed_models)
        return self._get_sorted_operations(operations, [*old_keys, *new_keys])

    def _get_sorted_operations(self, operations: list[HareOperation], keys: list[ModelKey]) -> list[HareOperation]:
        """The operations ordered by what each needs (``OperationSorter``), each in the app of the model it
        names.

        Args:
            operations: The operations, in the order they were made.
            keys: The compared models of both states.

        Returns:
            The ordered operations.
        """
        app_label_by_model_name: dict[str, str] = {}
        for app_label, model_name in keys:
            app_label_by_model_name.setdefault(model_name.lower(), app_label)
        default_app_label = keys[0][0] if keys else ""
        entries: list[tuple[str, Operation]] = [
            (
                next(
                    (
                        app_label_by_model_name[model_name.lower()]
                        for model_name in operation.get_table_model_names()
                        if model_name.lower() in app_label_by_model_name
                    ),
                    default_app_label,
                ),
                operation,
            )
            for operation in operations
        ]
        return [
            cast("HareOperation", operation) for _app_label, operation in OperationSorter(self.old_state).sort(entries)
        ]

    def _add_removals_of_relations_through_changed_keys(
        self, operations: list[HareOperation], old_keys: list[ModelKey], renamed_models: dict[ModelKey, ModelKey]
    ) -> list[HareOperation]:
        """Gives a many-to-many relation through a model that loses the key to one of the relation's sides a
        RemoveField of its own when the relation's model is deleted - the relation goes before the key
        (``RemoveField`` requires it), and its model only after the key.

        Args:
            operations: The operations, in generated order.
            old_keys: The compared models of the old state.
            renamed_models: Each renamed model's new key -> its old key.

        Returns:
            The operations, each RemoveField added before the key's removal.
        """
        new_names = {old_key: new_key[1] for new_key, old_key in renamed_models.items()}
        ordered_operations = list(operations)
        for owner_key in old_keys:
            owner_name = new_names.get(owner_key, owner_key[1])
            for relation_name, relation in self.old_state.models[owner_key].fields.items():
                if not isinstance(relation, ManyToManyFieldInstance):
                    continue
                through_key = self._get_through_model_key(relation)
                through_model_state = self.old_state.models.get(through_key) if through_key is not None else None
                if through_key is None or through_model_state is None:
                    continue
                side_keys = {owner_key, self._get_relation_target_key(relation)}
                through_name = new_names.get(through_key, through_key[1])
                key_removal_index = next(
                    (
                        position
                        for position, operation in enumerate(ordered_operations)
                        if isinstance(operation, RemoveField)
                        and operation.model_name == through_name
                        and operation.name in through_model_state.fields
                        and self._get_relation_target_key(through_model_state.fields[operation.name]) in side_keys
                    ),
                    None,
                )
                if key_removal_index is None:
                    continue
                later_operations = ordered_operations[key_removal_index + 1 :]
                relation_removal = next(
                    (
                        operation
                        for operation in ordered_operations
                        if isinstance(operation, RemoveField)
                        and operation.model_name == owner_name
                        and operation.name == relation_name
                    ),
                    None,
                )
                if relation_removal is None and any(
                    isinstance(operation, DeleteModel) and operation.name == owner_name
                    for operation in later_operations
                ):
                    ordered_operations.insert(
                        key_removal_index, RemoveField(model_name=owner_name, name=relation_name)
                    )
        return ordered_operations

    def _get_prerequisite_operations(
        self, old_keys: list[ModelKey], new_keys: list[ModelKey]
    ) -> tuple[list[HareOperation], list[HareOperation]]:
        """The operations on what tables are made of - schemas, extensions and ENUM types - scoped
        to the selected apps: only what their own models need is created, since one any old model
        (whatever its app) uses exists already; dropping mirrors it.

        Args:
            old_keys: The selected models of the old state.
            new_keys: The selected models of the new state.

        Returns:
            The operations run before any table is created or changed, and the ones dropping what
            nothing uses any more - run after every model and field is removed.
        """
        operations: list[HareOperation] = []
        # What exists already: what the operations created (kept in the state, though no model uses it
        # any more) and what the old models use. A schema no model uses any more stays - it may hold
        # other objects; a hand-written DropSchema drops it.
        old_schemas = self.old_state.schemas | self._collect_schemas(self.old_state)
        selected_new_schemas = self._collect_schemas(self.new_state, new_keys)
        operations.extend(CreateSchema(schema_name=schema) for schema in sorted(selected_new_schemas - old_schemas))

        # Before any CreateModel: a column of a type an extension provides (CITEXT) can't be
        # created until its extension exists. An extension no model declares any more stays - the
        # database may use it elsewhere; a hand-written RemoveExtension drops it.
        old_extensions = self.old_state.extensions | self._collect_extensions(self.old_state)
        selected_new_extensions = self._collect_extensions(self.new_state, new_keys)
        operations.extend(
            CreateExtension(extension_name=extension) for extension in sorted(selected_new_extensions - old_extensions)
        )

        # ENUM types, before the columns of them - created, or given their new labels (a removed
        # label converts the columns, whose values must not hold it any more).
        # A type the operations created, though no column is of it any more, needs no CREATE.
        old_enum_types = {**self.old_state.enum_types, **self._collect_enum_types(self.old_state)}
        new_enum_types = self._collect_enum_types(self.new_state)
        selected_old_enum_types = self._collect_enum_types(self.old_state, old_keys)
        selected_new_enum_types = self._collect_enum_types(self.new_state, new_keys)
        for type_key, enum_type in sorted(selected_new_enum_types.items(), key=self._get_enum_type_order):
            old_enum_type = old_enum_types.get(type_key)
            if old_enum_type is None:
                operations.append(
                    CreateEnumType(name=enum_type.name, labels=enum_type.labels, schema=enum_type.schema)
                )
            elif old_enum_type.labels != enum_type.labels:
                operations.append(
                    AlterEnumType(
                        name=enum_type.name,
                        old_labels=old_enum_type.labels,
                        new_labels=enum_type.labels,
                        schema=enum_type.schema,
                    )
                )

        cleanup_operations: list[HareOperation] = [
            DropEnumType(name=enum_type.name, labels=enum_type.labels, schema=enum_type.schema)
            for type_key, enum_type in sorted(selected_old_enum_types.items(), key=self._get_enum_type_order)
            if type_key not in new_enum_types
        ]
        return operations, cleanup_operations

    def _get_model_diff_operations(
        self, new_keys: list[ModelKey], renamed_models: dict[ModelKey, ModelKey]
    ) -> tuple[list[HareOperation], dict[ModelKey, list[HareOperation]]]:
        """The operations changing the models both states have, with the warnings of each.

        Args:
            new_keys: The selected models of the new state.
            renamed_models: The old key of each renamed model, by its new key.

        Returns:
            The operations, and the field operations of each model, by its new key.
        """
        model_diff_operations: list[HareOperation] = []
        field_operations_by_key: dict[ModelKey, list[HareOperation]] = {}
        # A renamed model is compared as its RenameModel leaves it - its table and its relations to
        # itself follow the new name.
        renamed_old_state = self.old_state.clone()
        for new_key, old_key in sorted(renamed_models.items()):
            RenameModel(old_name=old_key[1], new_name=new_key[1]).state_forward(new_key[0], renamed_old_state)
        old_model_states: dict[ModelKey, ModelState] = {}
        model_diffs: dict[ModelKey, StateModelDiff] = {}
        for new_key in new_keys:
            old_key = renamed_models.get(new_key, new_key)
            if old_key not in self.old_state.models:
                continue
            old_model_states[new_key] = (
                renamed_old_state.models[new_key] if new_key in renamed_models else self.old_state.models[old_key]
            )
            model_diffs[new_key] = StateModelDiff(
                old_model_states[new_key], self.new_state.models[new_key], self.rename_questions
            )
            model_diffs[new_key].generate_operations()
        # A relation naming a renamed field in its to_field is pointed at the new name by the RenameField
        # itself - compared as the field renames leave it, it isn't changed.
        field_renames = [
            (new_key, operation)
            for new_key, model_diff in model_diffs.items()
            for operation in model_diff.field_operations
            if isinstance(operation, RenameField)
        ]
        if field_renames:
            fields_renamed_state = renamed_old_state.clone()
            for new_key, rename in field_renames:
                rename.state_forward(new_key[0], fields_renamed_state)
            for new_key, old_model_state in old_model_states.items():
                relations_after_renames = self._get_relations_after_field_renames(
                    old_model_state, fields_renamed_state.models[new_key]
                )
                if relations_after_renames is not old_model_state:
                    model_diffs[new_key] = StateModelDiff(
                        relations_after_renames, self.new_state.models[new_key], self.rename_questions
                    )
                    model_diffs[new_key].generate_operations()
        for new_key, model_diff in model_diffs.items():
            model_diff_operations.extend(model_diff.operations)
            field_operations_by_key[new_key] = model_diff.field_operations
            self.warnings.extend(model_diff.warnings)
            self.data_loss_warnings.extend(model_diff.data_loss_warnings)
        return model_diff_operations, field_operations_by_key

    @staticmethod
    def _get_relations_after_field_renames(old_model_state: ModelState, renamed_model_state: ModelState) -> ModelState:
        """A model's old state with the ``to_field`` of each relation as the field renames of the run leave
        it.

        Args:
            old_model_state: The model's old state.
            renamed_model_state: The model's state after the field renames.

        Returns:
            A copy with the relations changed - the state itself when no to_field changes.
        """
        changed_relations: dict[str, Field[Any]] = {}
        for field_name, field in old_model_state.fields.items():
            renamed_field = renamed_model_state.fields.get(field_name)
            if (
                isinstance(field, ForeignKeyFieldInstance)
                and isinstance(renamed_field, ForeignKeyFieldInstance)
                and renamed_field.to_field != field.to_field
            ):
                relation = copy(field)
                relation.to_field = renamed_field.to_field
                changed_relations[field_name] = relation
        if not changed_relations:
            return old_model_state
        changed_state = old_model_state.clone()
        for field_name, changed_relation in changed_relations.items():
            changed_state.set_field(field_name, changed_relation)
        return changed_state

    def _get_schema_object_operations(
        self,
        model_diff_operations: list[HareOperation],
        field_operations_by_key: dict[ModelKey, list[HareOperation]],
        old_keys: list[ModelKey],
        new_keys: list[ModelKey],
        renamed_models: dict[ModelKey, ModelKey],
    ) -> SchemaObjectOperations:
        """The operations on what the models declare beside their tables: dropped before any column
        changes, created after every table is in place - a changed one is altered only when no column or
        table changes, and one reading a changed column is dropped and created again. An object is
        compared as the field renames leave the old one: an object naming a renamed field follows the
        rename.

        Args:
            model_diff_operations: The operations changing the models both states have.
            field_operations_by_key: The field operations of each model both states have, by its new key.
            old_keys: The selected models of the old state.
            new_keys: The selected models of the new state.
            renamed_models: The old key of each renamed model, by its new key.

        Returns:
            The operations, by the moment they run at.
        """
        tables_change = any(key not in new_keys for key in old_keys) or any(
            isinstance(operation, COLUMN_CHANGING_OPERATIONS) for operation in model_diff_operations
        )
        changed_columns = self._get_changed_columns(field_operations_by_key, old_keys, new_keys, renamed_models)
        schema_object_operations = SchemaObjectOperations(
            alters_in_place=not tables_change, changed_columns=frozenset(changed_columns)
        )
        for new_key in new_keys:
            new_model_state = self.new_state.models[new_key]
            if new_model_state.options.get(ModelOption.MANAGED) is False:
                continue
            old_key = renamed_models.get(new_key, new_key)
            if new_key in field_operations_by_key:
                old_model_state = StateModelDiff.get_state_after_field_renames(
                    self.old_state.models[old_key], field_operations_by_key[new_key]
                )
                schema_object_operations.add_changed_model(old_model_state, new_model_state)
            elif self._get_moved_model_source(new_key) is None:
                schema_object_operations.add_created_model(new_model_state)
        return schema_object_operations

    def _get_changed_columns(
        self,
        field_operations_by_key: dict[ModelKey, list[HareOperation]],
        old_keys: list[ModelKey],
        new_keys: list[ModelKey],
        renamed_models: dict[ModelKey, ModelKey],
    ) -> set[tuple[str, str]]:
        """The columns the migration changes: those its field operations change, every column of a
        deleted model's table, and the columns of a view or materialized view dropped and created again - its
        declaration changes or it reads one of them; a view reading it is dropped first in turn.

        Args:
            field_operations_by_key: The field operations of each model both states have, by its new key.
            old_keys: The selected models of the old state.
            new_keys: The selected models of the new state.
            renamed_models: The old key of each renamed model, by its new key.

        Returns:
            ``(table, column)`` of each column.
        """
        changed_columns: set[tuple[str, str]] = set()
        for new_key, field_operations in field_operations_by_key.items():
            old_model_state = self.old_state.models[renamed_models.get(new_key, new_key)]
            changed_columns |= StateModelDiff.get_changed_columns(old_model_state, field_operations)
        renamed_old_keys = set(renamed_models.values())
        for old_key in old_keys:
            if old_key in new_keys or old_key in renamed_old_keys:
                continue
            old_model_state = self.old_state.models[old_key]
            table = old_model_state.table or old_model_state.name.lower()
            for field_name, field in old_model_state.fields.items():
                column = ModelState.get_field_db_column(field_name, field)
                if column is not None:
                    changed_columns.add((table, column))
        return self._add_read_view_columns(changed_columns, old_keys, new_keys)

    def _add_read_view_columns(
        self, changed_columns: set[tuple[str, str]], old_keys: list[ModelKey], new_keys: list[ModelKey]
    ) -> set[tuple[str, str]]:
        """Adds the columns that views read of every view and materialized view dropped and created again -
        one whose declaration changes, one reading a changed column, through other views too: a view reading
        it is dropped first and created again after it.

        Args:
            changed_columns: The columns changed so far.
            old_keys: The selected models of the old state.
            new_keys: The selected models of the new state.

        Returns:
            ``changed_columns``, with the columns of the views made again.
        """
        views = self._get_views(self.old_state, old_keys)
        new_declarations = {
            view.name: ComparableValues.get_declaration_key(view, without_name=True)
            for view in self._get_views(self.new_state, new_keys)
        }
        # A view whose declaration changes is dropped and created again - the views reading it with it.
        made_again_names = {
            view.name
            for view in views
            if view.name in new_declarations
            and ComparableValues.get_declaration_key(view, without_name=True) != new_declarations[view.name]
        }
        while True:
            new_names = {
                view.name
                for view in views
                if view.name not in made_again_names
                and any(column in changed_columns or column[0] in made_again_names for column in view.read_columns)
            }
            if not new_names:
                break
            made_again_names |= new_names
        changed_columns.update(
            column for view in views for column in view.read_columns if column[0] in made_again_names
        )
        return changed_columns

    @staticmethod
    def _get_views(state: State, keys: list[ModelKey]) -> list[Any]:
        """The views and materialized views the models of a state declare.

        Args:
            state: The state.
            keys: The models.

        Returns:
            The views.
        """
        return [
            view
            for key in keys
            for option in (ModelOption.VIEWS, ModelOption.MATERIALIZED_VIEWS)
            for view in state.models[key].get_option_list(option)
        ]

    def _get_added_model_operations(self, added_keys: list[ModelKey]) -> list[HareOperation]:
        """The operations creating the models only the new state has.

        Args:
            added_keys: The models only the new state has.

        Returns:
            The operations.
        """
        operations: list[HareOperation] = []
        created_keys = []
        # A model moved here from another app keeps its existing table - recorded in the state
        # only, and never split up for a relation cycle (that would add an existing column).
        for new_key in added_keys:
            source_key = self._get_moved_model_source(new_key)
            if source_key is None:
                created_keys.append(new_key)
                continue
            self.moved_models[new_key] = source_key
            moved_model_operation = self.get_create_model_operation(self.new_state.models[new_key])
            moved_model_operation.state_only = True
            # The table it keeps is made by the migrations of the app it comes from.
            moved_model_operation.generation_dependencies = (
                OperationDependency.of_model(*source_key, DependencyType.CREATED),
            )
            operations.append(moved_model_operation)
        # Their order, and a relation taken out of one for a cycle, are OperationSorter's.
        for new_key in created_keys:
            create_model_operation = self.get_create_model_operation(self.new_state.models[new_key])
            create_model_operation.options = SchemaObjectOperations.without_deferred_options(
                create_model_operation.options or {}
            )
            operations.append(create_model_operation)
        return operations

    def _get_deleted_model_operations(
        self,
        deleted_keys: list[ModelKey],
        moved_out_keys: set[ModelKey],
        early_removed_fields: Collection[tuple[ModelKey, str]],
    ) -> list[HareOperation]:
        """The operations dropping the models only the old state has - before what they depend on,
        sorted against the old state. A cycle among them loses its deferred fields first.

        Args:
            deleted_keys: The models only the old state has.
            moved_out_keys: Those of them moved to another app - they keep their table.
            early_removed_fields: The relation fields already removed.

        Returns:
            The operations.

        Raises:
            ConfigurationError: A model moved to another app forms a relation cycle with models
                deleted in the same run.
        """
        operations: list[HareOperation] = []
        ordered_deleted_keys, deferred_delete_fields = self._sort_by_dependencies(deleted_keys, self.old_state)
        for key, field_name in deferred_delete_fields:
            if (key, field_name) in early_removed_fields:
                continue
            if key in moved_out_keys:
                raise ConfigurationError(
                    f"Model {key[0]}.{key[1]} is moved to another app, but its field {field_name!r} "
                    "forms a relation cycle with models deleted in the same run - delete those "
                    "models in a separate makemigrations run first."
                )
            operations.append(RemoveField(model_name=self.old_state.models[key].name, name=field_name))
        operations.extend(
            DeleteModel(name=old_key[1], state_only=old_key in moved_out_keys)
            for old_key in reversed(ordered_deleted_keys)
        )
        return operations
