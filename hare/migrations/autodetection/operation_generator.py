from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Sequence
from copy import copy
from typing import Any, cast

from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.schema_objects.enum_type import EnumType
from hare.dialects.dialect_registry import DialectRegistry
from hare.exceptions import ConfigurationError
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.migrations.autodetection.constants import (
    MANY_TO_MANY_THROUGH_TABLE_SWAP_SUFFIX,
    MODEL_RENAME_FIELD_SIMILARITY_THRESHOLD,
)
from hare.migrations.autodetection.diffs.state_model_diff import StateModelDiff
from hare.migrations.autodetection.schema_object_operations import SchemaObjectOperations
from hare.migrations.autodetection.state_signatures import StateSignatures
from hare.migrations.operations import (
    AddField,
    AddIndex,
    AlterEnumType,
    AlterField,
    CreateEnumType,
    CreateExtension,
    CreateModel,
    CreateSchema,
    DeleteModel,
    DropEnumType,
    DropSchema,
    HareOperation,
    RemoveExtension,
    RemoveField,
    RenameField,
    RenameModel,
)
from hare.migrations.operations.fields.field_like import FieldLike
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.models.enums import ModelOption

ModelKey = tuple[str, str]


class OperationGenerator:
    """Generate migration operations by comparing two State snapshots."""

    def __init__(
        self, old_state: State, new_state: State, connection_by_app: dict[str, str | None] | None = None
    ) -> None:
        """
        Args:
            old_state: The state the migration files on disk produce.
            new_state: The state of the current models.
            connection_by_app: Each app's default connection name - a table of one app is the
                same table as another app's only when both use the same connection.
        """
        self.old_state = old_state
        self.new_state = new_state
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

    @staticmethod
    def _non_field_signature(signature: dict[str, object]) -> dict[str, object]:
        return {key: value for key, value in signature.items() if key != "fields"}

    @staticmethod
    def _signature_fields(signature: dict[str, object]) -> list[str]:
        return cast("list[str]", signature["fields"])

    @staticmethod
    def _field_content_similarity(old_fields: list[str], new_fields: list[str]) -> float:
        """The fraction of the larger side's field content signatures also present in the other side -
        1.0 for an exact match, two empty lists included.
        """
        old_counts, new_counts = Counter(old_fields), Counter(new_fields)
        larger_side = max(sum(old_counts.values()), sum(new_counts.values()))
        if larger_side == 0:
            return 1.0
        common = sum((old_counts & new_counts).values())
        return common / larger_side

    def _match_renamed_models(self, old_keys: list[ModelKey], new_keys: list[ModelKey]) -> dict[ModelKey, ModelKey]:
        renamed: dict[ModelKey, ModelKey] = {}
        removed_keys = [key for key in old_keys if key not in new_keys]
        added_keys = [key for key in new_keys if key not in old_keys]
        # Each model's signature once - the loops below compare every pair.
        old_signatures = {key: StateSignatures.get_model_signature(self.old_state.models[key]) for key in removed_keys}
        new_signatures = {key: StateSignatures.get_model_signature(self.new_state.models[key]) for key in added_keys}

        for new_key in sorted(added_keys):
            new_sig = new_signatures[new_key]
            # A rename only when exactly one added and one removed model share the signature - a
            # wrong pairing would put rows under the wrong model.
            matching_added = [key for key in added_keys if key[0] == new_key[0] and new_signatures[key] == new_sig]
            if len(matching_added) != 1:
                continue
            matching_removed = [key for key in removed_keys if key[0] == new_key[0] and old_signatures[key] == new_sig]
            if len(matching_removed) != 1:
                continue
            old_key = matching_removed[0]
            renamed[new_key] = old_key
            removed_keys.remove(old_key)
            added_keys.remove(new_key)

        # A rename that also adds or removes a field: every part of the signature but the fields
        # must match, and most fields (MODEL_RENAME_FIELD_SIMILARITY_THRESHOLD) - unambiguously. The
        # differing fields then come out of the model diff as AddField/RemoveField.
        def is_similar_rename_candidate(candidate_key: ModelKey, other_rest: dict[str, object]) -> bool:
            return (
                self._non_field_signature(old_signatures[candidate_key]) == other_rest
                if candidate_key in removed_keys
                else self._non_field_signature(new_signatures[candidate_key]) == other_rest
            )

        for new_key in sorted(added_keys):
            new_signature = new_signatures[new_key]
            new_rest, new_fields = self._non_field_signature(new_signature), self._signature_fields(new_signature)
            matching_removed = [
                old_key
                for old_key in removed_keys
                if old_key[0] == new_key[0]
                and is_similar_rename_candidate(old_key, new_rest)
                and self._field_content_similarity(self._signature_fields(old_signatures[old_key]), new_fields)
                >= MODEL_RENAME_FIELD_SIMILARITY_THRESHOLD
            ]
            if len(matching_removed) != 1:
                continue
            old_key = matching_removed[0]
            old_fields = self._signature_fields(old_signatures[old_key])
            matching_added = [
                key
                for key in added_keys
                if key[0] == old_key[0]
                and is_similar_rename_candidate(key, new_rest)
                and self._field_content_similarity(old_fields, self._signature_fields(new_signatures[key]))
                >= MODEL_RENAME_FIELD_SIMILARITY_THRESHOLD
            ]
            if len(matching_added) != 1:
                continue
            renamed[new_key] = old_key
            removed_keys.remove(old_key)
            added_keys.remove(new_key)

        return renamed

    def _get_table_identity(self, key: ModelKey, model_state: ModelState) -> tuple[str | None, str | None, str]:
        """The connection, schema and table a model's rows live in.

        Args:
            key: The model's (app, model name) key.
            model_state: The model's state.

        Returns:
            The connection name, schema and table name.
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
        if not source_keys:
            return None
        if len(source_keys) > 1:
            source_names = ", ".join(f"{app}.{name}" for app, name in sorted(source_keys))
            raise ConfigurationError(
                f"Model {new_key[0]}.{new_key[1]} takes over table {table_identity[2]!r}, which several "
                f"removed models used ({source_names}) - move one model per makemigrations run."
            )
        source_key = source_keys[0]
        model_diff = StateModelDiff(self.old_state.models[source_key], new_model_state)
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
            model = state.apps.get_model(key[0], key[1])
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
        through_reference = SwappableModelReference.get_model_reference(many_to_many_field.through_model)
        if isinstance(through_reference, str):
            through_key = cast("ModelKey", tuple(through_reference.split(".", 1)))
        elif through_reference is not None:
            through_key = (through_reference._meta.app or "", through_reference.__name__)
        else:
            return None
        through_model_state = state.models.get(through_key)
        if through_model_state is None:
            return None
        return through_model_state.table or through_model_state.name.lower()

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
            related_name = field.related_name
            if target_key is None or related_name is False:
                continue
            if not related_name:
                related_name = f"{model_state.table or model_state.name.lower()}s"
            elif "%(app_label)s" in related_name or "%(class)s" in related_name:
                related_name = related_name % {"app_label": key[0], "class": model_state.name.lower()}
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
        for new_key in added_keys:
            new_signature = StateSignatures.get_model_signature(self.new_state.models[new_key])
            matching_removed_names = sorted(
                old_key[1]
                for old_key in removed_keys
                if old_key[0] == new_key[0]
                and StateSignatures.get_model_signature(self.old_state.models[old_key]) == new_signature
            )
            if not matching_removed_names:
                continue
            removed_names = ", ".join(repr(name) for name in matching_removed_names)
            warnings.append(
                f"Model {new_key[1]!r} (app {new_key[0]!r}) has the exact same fields/options as removed "
                f"model(s) {removed_names} - if it's actually a rename, the generated CreateModel/"
                "DeleteModel pair will DROP the old table's data instead of carrying it over. Replace "
                f"them by hand with RenameModel(old_name=..., new_name={new_key[1]!r}) if so."
            )
        return warnings

    @classmethod
    def get_to_field_references(
        cls, state: State, target_key: ModelKey, field_name: str
    ) -> list[tuple[ModelKey, str]]:
        """The relation fields of a state that point at a field through ``to_field``.

        Args:
            state: The state to look in.
            target_key: The referenced model's key.
            field_name: The referenced field's name.

        Returns:
            (model key, field name) of every such relation field.
        """
        references: list[tuple[ModelKey, str]] = []
        for model_key, model_state in state.models.items():
            for relation_name, relation_field in model_state.fields.items():
                if not isinstance(relation_field, ForeignKeyFieldInstance):
                    continue
                to_field = relation_field.to_field
                to_field_names = (to_field,) if isinstance(to_field, str) else tuple(to_field or ())
                if field_name in to_field_names and cls._get_relation_target_key(relation_field) == target_key:
                    references.append((model_key, relation_name))
        return references

    def _move_removals_after_referencing_operations(
        self, operations: list[HareOperation], new_keys: list[ModelKey], renamed_models: dict[ModelKey, ModelKey]
    ) -> tuple[list[HareOperation], list[HareOperation]]:
        """Moves each RemoveField of a field a same-app ``to_field`` relation points at to after
        the operations changing or removing that relation - removed first, the field would still
        be referenced.

        Args:
            operations: The model diff operations, in generated order.
            new_keys: The selected apps' models of the new state.
            renamed_models: New key -> old key of every renamed model.

        Returns:
            The reordered operations, and the RemoveField operations to run after the DeleteModel
            of a referencing model.
        """
        new_key_by_name = {key[1]: key for key in new_keys}
        new_name_by_old_key = {old_key: new_key[1] for new_key, old_key in renamed_models.items()}
        ordered_operations = list(operations)
        removals_after_deletions: list[HareOperation] = []
        for operation in operations:
            if not isinstance(operation, RemoveField) or operation.model_name not in new_key_by_name:
                continue
            new_key = new_key_by_name[operation.model_name]
            old_key = renamed_models.get(new_key, new_key)
            references = [
                (referencing_key, referencing_name)
                for referencing_key, referencing_name in self.get_to_field_references(
                    self.old_state, old_key, operation.name
                )
                if referencing_key[0] == old_key[0] and referencing_key != old_key
            ]
            removal_index = next(index for index, other in enumerate(ordered_operations) if other is operation)
            if any(
                referencing_key not in self.new_state.models and referencing_key not in new_name_by_old_key
                for referencing_key, _referencing_name in references
            ):
                del ordered_operations[removal_index]
                removals_after_deletions.append(operation)
                continue
            last_referencing_index = removal_index
            for referencing_key, referencing_name in references:
                referencing_model_name = new_name_by_old_key.get(referencing_key, referencing_key[1])
                referencing_names = {referencing_name}
                for index, other_operation in enumerate(ordered_operations):
                    if getattr(other_operation, "model_name", None) != referencing_model_name:
                        continue
                    if isinstance(other_operation, RenameField):
                        if other_operation.old_name not in referencing_names:
                            continue
                        referencing_names.add(other_operation.new_name)
                    elif getattr(other_operation, "name", None) not in referencing_names:
                        continue
                    last_referencing_index = max(last_referencing_index, index)
            if last_referencing_index == removal_index:
                continue
            # A later operation reusing the removed field's name must keep running after the removal.
            if any(
                getattr(other_operation, "model_name", None) == operation.model_name
                and operation.name
                in (getattr(other_operation, "name", None), getattr(other_operation, "new_name", None))
                for other_operation in ordered_operations[removal_index + 1 : last_referencing_index + 1]
            ):
                continue
            ordered_operations.insert(last_referencing_index + 1, operation)
            del ordered_operations[removal_index]
        return ordered_operations, removals_after_deletions

    @staticmethod
    def _create_model_operation(model_state: ModelState) -> CreateModel:
        fields: list[tuple[str, FieldLike]] = list(model_state.fields.items())
        return CreateModel(
            name=model_state.name,
            fields=fields,
            options=model_state.options,
            bases=[base.__name__ for base in model_state.bases],
        )

    @staticmethod
    def _sort_by_dependencies(keys: list[ModelKey], state: State) -> tuple[list[ModelKey], list[tuple[ModelKey, str]]]:
        """Sorts model keys so that relation targets come before the models pointing at them.

        Returns:
            The ordered keys, and the ``(model_key, field_name)`` pairs of a circular dependency no
            order resolves - created models get those fields by a later ``AddField``, deleted models
            lose them by a ``RemoveField`` first.
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
        """Collect all unique schema names used in a state.

        Args:
            state: The state to scan.
            keys: Only these models' schemas - every model of `state` when omitted.
        """
        schemas: set[str] = set()
        for key in state.models if keys is None else keys:
            schema = state.models[key].options.get(ModelOption.SCHEMA)
            if schema:
                schemas.add(schema)
        return schemas

    @staticmethod
    def _collect_extensions(state: State, keys: Iterable[ModelKey] | None = None) -> set[str]:
        """Collect every extension name a model in `state` needs - an explicit Meta.extensions
        entry, what any field's column needs on any dialect (``Field.get_required_extensions()``) and what an
        ExclusionConstraint needs on a dialect that has them (a GiST one's btree_gist).

        Args:
            state: The state to scan.
            keys: Only these models' extensions - every model of `state` when omitted.
        """
        extensions: set[str] = set()
        for key in state.models if keys is None else keys:
            model_state = state.models[key]
            extensions.update(model_state.options.get(ModelOption.EXTENSIONS, ()))
            for field in model_state.fields.values():
                extensions.update(field.get_required_extensions())
            # A migration file runs on any database - it creates what any dialect needs.
            for constraint in model_state.options.get(ModelOption.CONSTRAINTS, ()):
                if isinstance(constraint, UniqueConstraint) and constraint.without_overlaps:
                    extensions.update(
                        OperationGenerator._get_without_overlaps_extensions(constraint.fields, model_state)
                    )
                if not isinstance(constraint, ExclusionConstraint):
                    continue
                for dialect in DialectRegistry.get_dialects():
                    if dialect.features.supports_exclusion_constraints and (
                        constraint_extension
                        := dialect.schema_editor_class.constraint_statements_class.get_exclusion_constraint_extension(
                            constraint, model_state.fields
                        )
                    ):
                        extensions.add(constraint_extension)
            primary_key_attribute = model_state.options.get(ModelOption.PRIMARY_KEY_ATTRIBUTE)
            if model_state.options.get(ModelOption.PK_WITHOUT_OVERLAPS) and isinstance(primary_key_attribute, tuple):
                extensions.update(
                    OperationGenerator._get_without_overlaps_extensions(primary_key_attribute, model_state)
                )
        return extensions

    @staticmethod
    def _get_without_overlaps_extensions(field_names: Sequence[str], model_state: ModelState) -> set[str]:
        """The extensions any dialect needs for a key compared ``WITHOUT OVERLAPS``.

        Args:
            field_names: The key's fields, the range last.
            model_state: The key's model.

        Returns:
            The extension names.
        """
        return {
            key_extension
            for dialect in DialectRegistry.get_dialects()
            if dialect.features.supports_without_overlaps
            and (
                key_extension
                := dialect.schema_editor_class.constraint_statements_class.get_without_overlaps_extension(
                    field_names, model_state.fields
                )
            )
        }

    @staticmethod
    def _collect_enum_types(state: State, keys: Iterable[ModelKey] | None = None) -> dict[str, EnumType]:
        """The database ``ENUM`` types the fields of `state`'s models are of, by name.

        Args:
            state: The state to scan.
            keys: Only these models' types - every model of `state` when omitted.

        Raises:
            ConfigurationError: Two fields name one type with different labels.
        """
        enum_types: dict[str, EnumType] = {}
        for key in state.models if keys is None else keys:
            for field in state.models[key].fields.values():
                enum_type = field.requires_enum_type
                if enum_type is None:
                    continue
                known = enum_types.setdefault(enum_type.name, enum_type)
                if known.labels != enum_type.labels:
                    raise ConfigurationError(
                        f"Two fields name the ENUM type {enum_type.name!r} with different labels: "
                        f"{list(known.labels)} and {list(enum_type.labels)}"
                    )
        return enum_types

    def _get_changed_column_names(
        self, model_diff_operations: list[HareOperation], renamed_models: dict[ModelKey, ModelKey]
    ) -> set[str]:
        """The names and columns of the fields the model diffs remove or change.

        Args:
            model_diff_operations: The operations of the models in both states.
            renamed_models: New key -> old key of every renamed model.

        Returns:
            The field names and their columns.
        """
        old_key_by_new_name = {new_key[1]: old_key for new_key, old_key in renamed_models.items()}
        old_keys_by_name = {key[1]: key for key in self.old_state.models}
        names: set[str] = set()
        for operation in model_diff_operations:
            if not isinstance(operation, (RemoveField, AlterField)):
                continue
            names.add(operation.name)
            old_key = old_key_by_new_name.get(operation.model_name) or old_keys_by_name.get(operation.model_name)
            old_field = self.old_state.models[old_key].fields.get(operation.name) if old_key else None
            column = getattr(old_field, "source_field", None)
            if column:
                names.add(column)
        return names

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
        operations.extend(
            RenameModel(old_name=old_key[1], new_name=new_key[1])
            for new_key, old_key in sorted(renamed_models.items())
        )
        model_diff_operations = self._get_model_diff_operations(new_keys, renamed_models)
        schema_object_operations = self._get_schema_object_operations(model_diff_operations, new_keys, renamed_models)
        operations.extend(schema_object_operations.early_operations)
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
        ordered_model_diff_operations, removals_after_deletions = self._move_removals_after_referencing_operations(
            model_diff_operations, new_keys, renamed_models
        )
        operations.extend(ordered_model_diff_operations)
        through_table_restore_operations = self._swap_through_tables_for_automatic_relations(
            operations, new_keys, renamed_models
        )
        operations.extend(self._get_deleted_model_operations(deleted_keys, moved_out_keys, early_removed_fields))
        operations.extend(removals_after_deletions)
        operations.extend(through_table_restore_operations)
        operations.extend(schema_object_operations.late_operations)
        operations.extend(schema_object_operations.last_operations)
        operations.extend(cleanup_operations)
        return operations

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
        old_schemas = self._collect_schemas(self.old_state)
        new_schemas = self._collect_schemas(self.new_state)
        selected_old_schemas = self._collect_schemas(self.old_state, old_keys)
        selected_new_schemas = self._collect_schemas(self.new_state, new_keys)
        operations.extend(CreateSchema(schema_name=schema) for schema in sorted(selected_new_schemas - old_schemas))

        # Before any CreateModel: a column of a type an extension provides (CITEXT) can't be
        # created until its extension exists.
        old_extensions = self._collect_extensions(self.old_state)
        new_extensions = self._collect_extensions(self.new_state)
        selected_old_extensions = self._collect_extensions(self.old_state, old_keys)
        selected_new_extensions = self._collect_extensions(self.new_state, new_keys)
        operations.extend(
            CreateExtension(extension_name=extension) for extension in sorted(selected_new_extensions - old_extensions)
        )

        # ENUM types, before the columns of them - created, or given their new labels (a removed
        # label converts the columns, whose values must not hold it any more).
        old_enum_types = self._collect_enum_types(self.old_state)
        new_enum_types = self._collect_enum_types(self.new_state)
        selected_old_enum_types = self._collect_enum_types(self.old_state, old_keys)
        selected_new_enum_types = self._collect_enum_types(self.new_state, new_keys)
        for name, enum_type in sorted(selected_new_enum_types.items()):
            old_enum_type = old_enum_types.get(name)
            if old_enum_type is None:
                operations.append(CreateEnumType(name=name, labels=enum_type.labels))
            elif old_enum_type.labels != enum_type.labels:
                operations.append(
                    AlterEnumType(name=name, old_labels=old_enum_type.labels, new_labels=enum_type.labels)
                )

        cleanup_operations: list[HareOperation] = [
            DropEnumType(name=name, labels=enum_type.labels)
            for name, enum_type in sorted(selected_old_enum_types.items())
            if name not in new_enum_types
        ]
        cleanup_operations.extend(
            RemoveExtension(extension_name=extension) for extension in sorted(selected_old_extensions - new_extensions)
        )
        cleanup_operations.extend(
            DropSchema(schema_name=schema) for schema in sorted(selected_old_schemas - new_schemas)
        )
        return operations, cleanup_operations

    def _get_model_diff_operations(
        self, new_keys: list[ModelKey], renamed_models: dict[ModelKey, ModelKey]
    ) -> list[HareOperation]:
        """The operations changing the models both states have, with the warnings of each.

        Args:
            new_keys: The selected models of the new state.
            renamed_models: The old key of each renamed model, by its new key.

        Returns:
            The operations.
        """
        model_diff_operations: list[HareOperation] = []
        for new_key in new_keys:
            old_key = renamed_models.get(new_key, new_key)
            if old_key not in self.old_state.models:
                continue
            model_diff = StateModelDiff(self.old_state.models[old_key], self.new_state.models[new_key])
            model_diff_operations.extend(model_diff.generate_operations())
            self.warnings.extend(model_diff.warnings)
            self.data_loss_warnings.extend(model_diff.data_loss_warnings)
        return model_diff_operations

    def _get_schema_object_operations(
        self,
        model_diff_operations: list[HareOperation],
        new_keys: list[ModelKey],
        renamed_models: dict[ModelKey, ModelKey],
    ) -> SchemaObjectOperations:
        """The operations on what the models declare beside their tables: dropped before any column
        changes, created after every table is in place.

        Args:
            model_diff_operations: The operations changing the models both states have.
            new_keys: The selected models of the new state.
            renamed_models: The old key of each renamed model, by its new key.

        Returns:
            The operations, by the moment they run at.
        """
        schema_object_operations = SchemaObjectOperations(
            self._get_changed_column_names(model_diff_operations, renamed_models)
        )
        for new_key in new_keys:
            old_key = renamed_models.get(new_key, new_key)
            new_model_state = self.new_state.models[new_key]
            if new_model_state.options.get(ModelOption.MANAGED) is False:
                continue
            if old_key in self.old_state.models:
                schema_object_operations.add_changed_model(self.old_state.models[old_key], new_model_state)
            elif self._get_moved_model_source(new_key) is None:
                schema_object_operations.add_created_model(new_model_state)
        return schema_object_operations

    def _get_added_model_operations(self, added_keys: list[ModelKey]) -> list[HareOperation]:
        """The operations creating the models only the new state has, the ones others depend on
        first.

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
            moved_model_operation = self._create_model_operation(self.new_state.models[new_key])
            moved_model_operation.state_only = True
            operations.append(moved_model_operation)
        ordered_added_keys, deferred_fields = self._sort_by_dependencies(created_keys, self.new_state)
        create_model_operations: dict[ModelKey, CreateModel] = {}
        for new_key in ordered_added_keys:
            create_model_operation = self._create_model_operation(self.new_state.models[new_key])
            create_model_operation.options = SchemaObjectOperations.without_deferred_options(
                create_model_operation.options or {}
            )
            create_model_operations[new_key] = create_model_operation
            operations.append(create_model_operation)
        # The fields deferred out of CreateModel for a cycle - added now that every model exists.
        for key, field_name in deferred_fields:
            create_model_operation = create_model_operations[key]
            field, indexes = create_model_operation.pop_field(field_name)
            operations.append(AddField(model_name=create_model_operation.name, name=field_name, field=field))
            operations.extend(AddIndex(model_name=create_model_operation.name, index=index) for index in indexes)
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
