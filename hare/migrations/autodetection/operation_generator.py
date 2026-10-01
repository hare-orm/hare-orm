from collections import Counter
from collections.abc import Iterable, Sequence
from copy import copy
from typing import Any, cast

from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.dialects.registry import DialectRegistry
from hare.exceptions import ConfigurationError
from hare.fields.base.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.swappable import SwappableModelReference
from hare.migrations.autodetection.model_diff import StateModelDiff
from hare.migrations.autodetection.state_signatures import StateSignatures
from hare.migrations.constants import M2M_THROUGH_TABLE_SWAP_SUFFIX, MODEL_RENAME_FIELD_SIMILARITY_THRESHOLD
from hare.migrations.operations import (
    AddField,
    AddIndex,
    AlterField,
    CreateExtension,
    CreateModel,
    CreateSchema,
    DeleteModel,
    DropSchema,
    HareOperation,
    RemoveExtension,
    RemoveField,
    RenameField,
    RenameModel,
)
from hare.migrations.operations.fields.field_like import FieldLike
from hare.migrations.state.project.model_state import ModelState
from hare.migrations.state.project.state import State
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

    def _filter_keys(self, keys: Iterable[ModelKey], app_labels: Sequence[str] | None) -> list[ModelKey]:
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

    def _get_through_model_table(self, state: State, m2m_field: ManyToManyFieldInstance[Any]) -> str | None:
        """The table of the through model an M2M field declares in `state`.

        Args:
            state: The state the field belongs to.
            m2m_field: An M2M field declared with ``through=SomeModel``.

        Returns:
            The through model's table, or None when the model isn't part of `state`.
        """
        through_reference = SwappableModelReference.get_model_reference(m2m_field.through_model)
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
                swapped_field.through = f"{rendered_old_field.through}{M2M_THROUGH_TABLE_SWAP_SUFFIX}"
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
            swapped_field.through = f"{new_through}{M2M_THROUGH_TABLE_SWAP_SUFFIX}"
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

    def _create_model_operation(self, model_state: ModelState) -> CreateModel:
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
        field_deps: dict[ModelKey, dict[str, ModelKey]] = {k: {} for k in keys}
        for key in keys:
            model_state = state.models[key]
            for field_name, field in model_state.fields.items():
                if not isinstance(field, (ForeignKeyFieldInstance, ManyToManyFieldInstance)):
                    continue
                dep_key = OperationGenerator._get_relation_target_key(field)
                if dep_key is not None and dep_key in key_set and dep_key != key:
                    field_deps[key][field_name] = dep_key

        deps: dict[ModelKey, set[ModelKey]] = {k: set(v.values()) for k, v in field_deps.items()}

        # Kahn's algorithm
        result: list[ModelKey] = []
        in_degree = {k: len(v) for k, v in deps.items()}
        queue = sorted([k for k, d in in_degree.items() if d == 0])
        while queue:
            node = queue.pop(0)
            result.append(node)
            for k, d in deps.items():
                if node in d:
                    d.discard(node)
                    in_degree[k] -= 1
                    if in_degree[k] == 0:
                        queue.append(k)
                        queue.sort()

        # What is left is a cycle among `keys` - one node's dependencies are deferred at a time.
        deferred: list[tuple[ModelKey, str]] = []
        remaining = {k for k in keys if k not in result}
        while remaining:
            node = sorted(remaining)[0]
            for field_name, dep_key in sorted(field_deps[node].items()):
                if dep_key in deps[node]:
                    deferred.append((node, field_name))
                    deps[node].discard(dep_key)
            result.append(node)
            remaining.discard(node)
            for k in remaining:
                deps[k].discard(node)

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
        entry, any field's own requires_extension (e.g. CitextField) and what an
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
                if field.requires_extension:
                    extensions.add(field.requires_extension)
            for constraint in model_state.options.get(ModelOption.CONSTRAINTS, ()):
                if not isinstance(constraint, ExclusionConstraint):
                    continue
                # A migration file runs on any database - it creates what any dialect needs.
                for dialect in DialectRegistry.get_dialects():
                    if dialect.supports_exclusion_constraints and (
                        constraint_extension := dialect.get_exclusion_constraint_extension(
                            constraint, model_state.fields
                        )
                    ):
                        extensions.add(constraint_extension)
        return extensions

    def generate(self, app_labels: Sequence[str] | None = None) -> list[HareOperation]:
        old_keys = self._filter_keys(self.old_state.models.keys(), app_labels)
        new_keys = self._filter_keys(self.new_state.models.keys(), app_labels)
        renamed_models = self._match_renamed_models(old_keys, new_keys)
        renamed_old_keys = set(renamed_models.values())
        self.warnings.extend(self._get_unrecognized_model_rename_warnings(old_keys, new_keys, renamed_models))

        operations: list[HareOperation] = []

        # Detect new schemas that need creation (before any CreateModel) - only the ones the
        # selected apps' own models need; one any old model (whatever its app) uses exists
        # already. Dropping mirrors it: one a selected old model used, no new model uses.
        old_schemas = self._collect_schemas(self.old_state)
        new_schemas = self._collect_schemas(self.new_state)
        selected_old_schemas = self._collect_schemas(self.old_state, old_keys)
        selected_new_schemas = self._collect_schemas(self.new_state, new_keys)
        for schema in sorted(selected_new_schemas - old_schemas):
            operations.append(CreateSchema(schema_name=schema))

        # Detect new extensions that need creation (before any CreateModel - a column of a
        # Postgres-extension-provided type, e.g. CITEXT, can't be created until its extension
        # exists) - scoped to the selected apps the same way as schemas above.
        old_extensions = self._collect_extensions(self.old_state)
        new_extensions = self._collect_extensions(self.new_state)
        selected_old_extensions = self._collect_extensions(self.old_state, old_keys)
        selected_new_extensions = self._collect_extensions(self.new_state, new_keys)
        for extension in sorted(selected_new_extensions - old_extensions):
            operations.append(CreateExtension(extension_name=extension))

        for new_key, old_key in sorted(renamed_models.items()):
            operations.append(RenameModel(old_name=old_key[1], new_name=new_key[1]))

        operations.extend(self._get_through_table_swap_operations(new_keys, renamed_models))

        deleted_keys = [k for k in old_keys if k not in new_keys and k not in renamed_old_keys]
        moved_out_keys = {key for key in deleted_keys if self._get_moved_model_target(key) is not None}
        # A deleted model's relation whose backward accessor a new relation registers again has to
        # be gone before that new relation is created - the whole model when nothing else still
        # references it, else only the conflicting relation field.
        early_deleted_keys, early_removed_fields = self._get_backward_relation_conflicts(
            [key for key in deleted_keys if key not in moved_out_keys]
        )
        for key in early_deleted_keys:
            operations.append(DeleteModel(name=key[1]))
        for key, field_name in early_removed_fields:
            operations.append(RemoveField(model_name=key[1], name=field_name))
        deleted_keys = [key for key in deleted_keys if key not in early_deleted_keys]

        added_keys = [k for k in new_keys if k not in old_keys and k not in renamed_models]
        # A model moved here from another app keeps its existing table - recorded in the state
        # only, and never split up for a relation cycle (that would add an existing column).
        for new_key in list(added_keys):
            source_key = self._get_moved_model_source(new_key)
            if source_key is None:
                continue
            self.moved_models[new_key] = source_key
            added_keys.remove(new_key)
            moved_model_operation = self._create_model_operation(self.new_state.models[new_key])
            moved_model_operation.state_only = True
            operations.append(moved_model_operation)
        ordered_added_keys, deferred_fields = self._sort_by_dependencies(added_keys, self.new_state)
        create_model_operations: dict[ModelKey, CreateModel] = {}
        for new_key in ordered_added_keys:
            create_model_operations[new_key] = self._create_model_operation(self.new_state.models[new_key])
            operations.append(create_model_operations[new_key])
        # The fields deferred out of CreateModel for a cycle - added now that every model exists.
        for key, field_name in deferred_fields:
            create_model_operation = create_model_operations[key]
            field, indexes = create_model_operation.pop_field(field_name)
            operations.append(AddField(model_name=create_model_operation.name, name=field_name, field=field))
            operations.extend(AddIndex(model_name=create_model_operation.name, index=index) for index in indexes)

        model_diff_operations: list[HareOperation] = []
        for new_key in new_keys:
            old_key = renamed_models.get(new_key, new_key)
            if old_key not in self.old_state.models:
                continue
            model_diff = StateModelDiff(self.old_state.models[old_key], self.new_state.models[new_key])
            model_diff_operations.extend(model_diff.generate_operations())
            self.warnings.extend(model_diff.warnings)
            self.data_loss_warnings.extend(model_diff.data_loss_warnings)
        ordered_model_diff_operations, removals_after_deletions = self._move_removals_after_referencing_operations(
            model_diff_operations, new_keys, renamed_models
        )
        operations.extend(ordered_model_diff_operations)
        through_table_restore_operations = self._swap_through_tables_for_automatic_relations(
            operations, new_keys, renamed_models
        )

        # Deleted models are dropped before what they depend on, sorted against old_state. A cycle
        # among them loses its deferred fields first.
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
            model_state = self.old_state.models[key]
            operations.append(RemoveField(model_name=model_state.name, name=field_name))
        for old_key in reversed(ordered_deleted_keys):
            operations.append(DeleteModel(name=old_key[1], state_only=old_key in moved_out_keys))
        operations.extend(removals_after_deletions)
        operations.extend(through_table_restore_operations)

        # Extensions no longer needed, after every DeleteModel.
        for extension in sorted(selected_old_extensions - new_extensions):
            operations.append(RemoveExtension(extension_name=extension))

        # Schemas no longer used, after every DeleteModel.
        for schema in sorted(selected_old_schemas - new_schemas):
            operations.append(DropSchema(schema_name=schema))

        return operations
