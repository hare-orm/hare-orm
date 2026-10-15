from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any

from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.migrations.autodetection.state_signatures import StateSignatures
from hare.migrations.operations import (
    AddConstraint,
    AddField,
    AddIndex,
    AlterField,
    HareOperation,
    RemoveField,
    RenameField,
)
from hare.migrations.state.model_state import ModelState
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.fields.field import Field


class StateFieldDiff:
    def __init__(self, old_state: ModelState, new_state: ModelState) -> None:
        self.old_state = old_state
        self.new_state = new_state
        #: Advisories about an AddField/RemoveField pair that may be an unrecognized rename -
        #: printed by makemigrations, never changing the operations.
        self.warnings: list[str] = []
        #: Advisories that a generated field turned into a plain one loses its computed values -
        #: printed under their own header.
        self.data_loss_warnings: list[str] = []

    def _generated_field_recreate_operations(self, field_name: str, field: Field[Any]) -> list[HareOperation]:
        operations: list[HareOperation] = []
        if getattr(field, "index", False):
            operations.append(AddIndex(model_name=self.new_state.name, index=Index(fields=(field_name,))))

        old_indexes = StateSignatures.normalize_indexes(self.old_state.get_option_list(ModelOption.INDEXES))
        new_indexes = StateSignatures.normalize_indexes(self.new_state.get_option_list(ModelOption.INDEXES))
        old_index_sigs = {StateSignatures.get_index_signature(index) for index in old_indexes}
        for index in new_indexes:
            if StateSignatures.get_index_signature(index) not in old_index_sigs:
                continue
            if field_name in index.fields or field_name in index.include:
                operations.append(AddIndex(model_name=self.new_state.name, index=index))

        old_constraints = StateSignatures.normalize_constraints(
            self.old_state.get_option_list(ModelOption.CONSTRAINTS)
        )
        new_constraints = StateSignatures.normalize_constraints(
            self.new_state.get_option_list(ModelOption.CONSTRAINTS)
        )
        old_constraints_set = set(old_constraints)
        for constraint in new_constraints:
            if constraint not in old_constraints_set:
                continue
            if (
                isinstance(constraint, UniqueConstraint)
                and field_name in constraint.fields
                or isinstance(constraint, ExclusionConstraint)
                and field_name in [field for field, _operator in constraint.expressions]
            ):
                operations.append(AddConstraint(model_name=self.new_state.name, constraint=constraint))

        return operations

    @staticmethod
    def get_comparable_signatures(
        old_field: Field[Any], new_field: Field[Any]
    ) -> tuple[dict[str, object], dict[str, object]]:
        """The signatures of one field's two definitions, reduced to what both of them know.

        A relation replayed from a hand-written migration may leave ``to_field`` unset, meaning
        the target's primary key - only an explicit value on both sides can differ.

        Args:
            old_field: The field's previous definition.
            new_field: The field's new definition.

        Returns:
            The old and the new signature.
        """
        old_signature = StateSignatures.get_field_signature(old_field)
        new_signature = StateSignatures.get_field_signature(new_field)
        if old_signature.get("to_field") is None or new_signature.get("to_field") is None:
            old_signature.pop("to_field", None)
            new_signature.pop("to_field", None)
        return old_signature, new_signature

    def _append_narrowing_data_loss_warning(
        self, field_name: str, old_field: Field[Any], new_field: Field[Any]
    ) -> None:
        """Warns when ``max_length``, ``max_digits``/``decimal_places`` or the like is narrowed - the
        schema editor refuses it at apply time if a row would overflow; this tells at makemigrations
        time.

        Args:
            field_name: The field's name.
            old_field: The previous definition.
            new_field: The new definition.
        """
        if new_field.get_narrowing_limit(old_field) is None:
            return
        self.data_loss_warnings.append(
            f"Model {self.new_state.name}: narrowing field {field_name!r} (e.g. max_length/"
            "max_digits/decimal_places or a smaller type) can leave already-stored values that don't "
            "fit the new column type. The migration will refuse to apply if any existing row doesn't "
            "fit - review the data ahead of time if that's unexpected."
        )

    def _try_add_relation_rename(
        self,
        new_name: str,
        new_field: Field[Any],
        added_fields: set[str],
        removed_fields: set[str],
        operations: list[HareOperation],
    ) -> None:
        """Appends a ``RenameField`` and takes both names out of ``added_fields``/``removed_fields``
        when exactly one removed and one added relation field of the same type share ``new_field``'s
        rename signature.
        """
        old_fields = self.old_state.fields
        new_fields = self.new_state.fields
        relation_class = type(new_field)
        new_signature = StateSignatures.get_field_signature_for_rename(new_field)
        matching_removed = [
            name
            for name in removed_fields
            if type(old_fields[name]) is relation_class
            and StateSignatures.get_field_signature_for_rename(old_fields[name]) == new_signature
        ]
        if len(matching_removed) != 1:
            return
        matching_added = [
            name
            for name in added_fields
            if type(new_fields[name]) is relation_class
            and StateSignatures.get_field_signature_for_rename(new_fields[name]) == new_signature
        ]
        if len(matching_added) != 1:
            return
        old_name = matching_removed[0]
        operations.append(
            RenameField(model_name=self.new_state.name, old_name=old_name, new_name=new_name, field=new_field)
        )
        removed_fields.remove(old_name)
        added_fields.remove(new_name)

    def generate_operations(self) -> list[HareOperation]:
        operations: list[HareOperation] = []
        old_fields = self.old_state.fields
        new_fields = self.new_state.fields
        added_fields = set(new_fields) - set(old_fields)
        removed_fields = set(old_fields) - set(new_fields)
        # A recreated generated field can depend on a field added in this same change, so it's
        # added back (with its indexes/constraints) only after every added field exists.
        generated_field_additions: list[HareOperation] = []
        self._add_renamed_fields(added_fields, removed_fields, operations, generated_field_additions)
        common_names = sorted(set(old_fields) & set(new_fields))
        self._raise_if_fields_swap_columns(common_names)
        self._add_altered_fields(common_names, operations, generated_field_additions)
        self._warn_about_possible_renames(added_fields, removed_fields)

        # A removed relation goes first: an added relation to the same target model would
        # otherwise collide with its still-registered backward accessor.
        relation_classes = (ForeignKeyFieldInstance, ManyToManyFieldInstance)
        operations.extend(
            RemoveField(model_name=self.new_state.name, name=name)
            for name in sorted(removed_fields)
            if isinstance(old_fields[name], relation_classes)
        )

        for name in sorted(added_fields):
            new_field = new_fields[name]
            operations.append(AddField(model_name=self.new_state.name, name=name, field=new_field))
            # A field's index=True needs no AddIndex here - the model state carries its implicit
            # index, and the index diff adds it.

        operations.extend(generated_field_additions)

        operations.extend(
            RemoveField(model_name=self.new_state.name, name=name)
            for name in sorted(removed_fields)
            if not isinstance(old_fields[name], relation_classes)
        )

        return operations

    def _add_renamed_fields(
        self,
        added_fields: set[str],
        removed_fields: set[str],
        operations: list[HareOperation],
        generated_field_additions: list[HareOperation],
    ) -> None:
        """Adds the operations of each added field that renames a removed one - found by the column
        both name, or by a relation's rename signature - and takes the pair out of the added and
        removed fields.

        Args:
            added_fields: The names of the added fields.
            removed_fields: The names of the removed fields.
            operations: The operations - the renames are appended.
            generated_field_additions: The operations adding generated fields back after every
                added field - a renamed generated field recreated is appended.
        """
        old_fields = self.old_state.fields
        new_fields = self.new_state.fields
        for new_name in sorted(added_fields):
            new_field = new_fields[new_name]
            new_source_field = getattr(new_field, "source_field", None)
            is_direct_relation = isinstance(new_field, ForeignKeyFieldInstance)
            # A rename is trusted only when the added field's source_field names the removed field's
            # column - its type may change in the same edit.
            if new_source_field is None:
                # A many-to-many field has no column; it and a forward relation are matched by their
                # rename signature - otherwise the through table would be dropped and created again.
                if isinstance(new_field, (ManyToManyFieldInstance, ForeignKeyFieldInstance)):
                    self._try_add_relation_rename(new_name, new_field, added_fields, removed_fields, operations)
                continue
            # Unambiguous on both sides: exactly one added and one removed field claim the column,
            # recomputed against the shrinking pools. Columns are compared - a foreign key's
            # defaults to "<name>_id".
            new_column = ModelState.get_field_db_column(new_name, new_field)
            matching_added = [
                name
                for name in added_fields
                if getattr(new_fields[name], "source_field", None) is not None
                and ModelState.get_field_db_column(name, new_fields[name]) == new_column
            ]
            matching_removed = [
                name for name in removed_fields if ModelState.get_field_db_column(name, old_fields[name]) == new_column
            ]
            if len(matching_added) != 1 or len(matching_removed) != 1:
                if is_direct_relation:
                    self._try_add_relation_rename(new_name, new_field, added_fields, removed_fields, operations)
                continue
            old_name = matching_removed[0]
            old_field = old_fields[old_name]
            if StateSignatures.get_field_signature_for_rename(
                old_field
            ) == StateSignatures.get_field_signature_for_rename(new_field):
                operations.append(
                    RenameField(
                        model_name=self.new_state.name,
                        old_name=old_name,
                        new_name=new_name,
                        field=new_field,
                    )
                )
            elif not (old_field.pk or new_field.pk) and (old_field.generated or new_field.generated):
                # A generated column whose type changes too is dropped and created under the new
                # name. Not an auto-increment primary key, which also counts as generated -
                # recreating it would break the foreign keys to it.
                operations.append(RemoveField(model_name=self.new_state.name, name=old_name))
                generated_field_additions.append(
                    AddField(model_name=self.new_state.name, name=new_name, field=new_field)
                )
                generated_field_additions.extend(self._generated_field_recreate_operations(new_name, new_field))
                if old_field.generated and not new_field.generated:
                    # The generated column's values are dropped with it - nothing carries over.
                    self.data_loss_warnings.append(
                        f"Model {self.new_state.name}: renaming generated field {old_name!r} to "
                        f"{new_name!r} while also turning it into a plain field will DROP its "
                        f"already-computed values - RemoveField+AddField has no way to carry them "
                        "over. Add a BackfillColumn (or RunPython) step after this migration if "
                        "the old computed values need to be preserved in the new column."
                    )
            else:
                # Renamed first onto a field keeping the old type: RenameField writes the full
                # target field into the state, and the AlterField after it would see no change.
                intermediate_field = copy(old_field)
                intermediate_field.source_field = new_source_field
                operations.append(
                    RenameField(
                        model_name=self.new_state.name,
                        old_name=old_name,
                        new_name=new_name,
                        field=intermediate_field,
                    )
                )
                self._append_narrowing_data_loss_warning(new_name, old_field, new_field)
                operations.append(AlterField(model_name=self.new_state.name, name=new_name, field=new_field))
            removed_fields.remove(old_name)
            added_fields.remove(new_name)

    def _raise_if_fields_swap_columns(self, common_names: list[str]) -> None:
        """Refuses fields swapping columns with each other - that would need a temporary third name.

        Args:
            common_names: The names of the fields both states have.

        Raises:
            ConfigurationError: Fields swap columns.
        """
        old_fields = self.old_state.fields
        new_fields = self.new_state.fields
        # Rejected at makemigrations time instead of failing at migrate time.
        new_columns_by_name = {
            name: (new_fields[name].source_field or name)
            for name in common_names
            if (old_fields[name].source_field or name) != (new_fields[name].source_field or name)
        }
        old_columns_changing = {(old_fields[name].source_field or name) for name in new_columns_by_name}
        colliding_names = sorted(
            name for name, new_column in new_columns_by_name.items() if new_column in old_columns_changing
        )
        if colliding_names:
            raise ConfigurationError(
                f"Fields {colliding_names} on model {self.new_state.name} are swapping DB columns "
                "with each other in the same change - this isn't automatically migratable (every "
                "ordering of the resulting RENAME COLUMN statements collides with a column another "
                "one of them hasn't vacated yet). Split it into two migrations by hand instead: "
                "rename one of them onto a temporary column name first, then rename the rest "
                "(including that temporary name onto its real target) in a second migration."
            )

    def _add_altered_fields(
        self,
        common_names: list[str],
        operations: list[HareOperation],
        generated_field_additions: list[HareOperation],
    ) -> None:
        """Adds the operations of each field both states have whose definition changed.

        Args:
            common_names: The names of the fields both states have.
            operations: The operations - the changes are appended.
            generated_field_additions: The operations adding generated fields back after every
                added field - a changed generated field recreated is appended.

        Raises:
            ConfigurationError: A field changes between a foreign key and a many-to-many relation.
        """
        old_fields = self.old_state.fields
        new_fields = self.new_state.fields
        for name in common_names:
            old_sig, new_sig = self.get_comparable_signatures(old_fields[name], new_fields[name])
            if old_sig != new_sig:
                old_field = old_fields[name]
                new_field = new_fields[name]
                old_is_foreign_key = isinstance(old_field, ForeignKeyFieldInstance)
                new_is_foreign_key = isinstance(new_field, ForeignKeyFieldInstance)
                old_is_many_to_many = isinstance(old_field, ManyToManyFieldInstance)
                new_is_many_to_many = isinstance(new_field, ManyToManyFieldInstance)
                if (old_is_foreign_key and new_is_many_to_many) or (old_is_many_to_many and new_is_foreign_key):
                    # FK and M2M have entirely different physical schemas - a column plus a
                    # constraint versus a separate through table - there's no in-place ALTER
                    # that could carry one into the other, automatically or otherwise.
                    raise ConfigurationError(
                        f"Field '{name}' on model {self.new_state.name} is changing between a "
                        "ForeignKeyField and a ManyToManyField - this isn't automatically "
                        "migratable. Split it into two migrations by hand instead: remove the "
                        "old field in one migration, then add the new field in a separate one."
                    )
                # Not an auto-increment primary key, which also counts as generated.
                if not (old_field.pk or new_field.pk) and (old_field.generated or new_field.generated):
                    operations.append(RemoveField(model_name=self.new_state.name, name=name))
                    generated_field_additions.append(
                        AddField(model_name=self.new_state.name, name=name, field=new_field)
                    )
                    generated_field_additions.extend(self._generated_field_recreate_operations(name, new_field))
                    if old_field.generated and not new_field.generated:
                        # Same data-loss risk as the rename-detected branch above, just without a
                        # rename involved - see that branch's own comment for why only this
                        # direction (not plain->generated) needs the warning.
                        self.data_loss_warnings.append(
                            f"Model {self.new_state.name}: turning generated field {name!r} into "
                            "a plain field will DROP its already-computed values - RemoveField+"
                            "AddField has no way to carry them over. Add a BackfillColumn (or "
                            "RunPython) step after this migration if the old computed values need "
                            "to be preserved in the new column."
                        )
                else:
                    self._append_narrowing_data_loss_warning(name, old_field, new_field)
                    if old_is_foreign_key and old_sig.get("to_field") != new_sig.get("to_field"):
                        self.data_loss_warnings.append(
                            f"Model {self.new_state.name}: field {name!r} now references another target "
                            "column (to_field). Stored key values can't be translated, so the migration "
                            "refuses to apply while any row stores one - migrate those rows by hand."
                        )
                    operations.append(AlterField(model_name=self.new_state.name, name=name, field=new_field))

    def _warn_about_possible_renames(self, added_fields: set[str], removed_fields: set[str]) -> None:
        """Warns about each added field with the very signature of exactly one removed one - a rename
        that also moved the column, maybe; never guessed.

        Args:
            added_fields: The names of the added fields left.
            removed_fields: The names of the removed fields left.
        """
        old_fields = self.old_state.fields
        new_fields = self.new_state.fields
        for name in sorted(added_fields):
            new_rename_signature = StateSignatures.get_field_signature_for_rename(new_fields[name])
            matching_removed_by_signature = sorted(
                removed_name
                for removed_name in removed_fields
                if StateSignatures.get_field_signature_for_rename(old_fields[removed_name]) == new_rename_signature
            )
            if len(matching_removed_by_signature) == 1:
                old_name = matching_removed_by_signature[0]
                self.warnings.append(
                    f"Model {self.new_state.name}: added field {name!r} has the exact same "
                    f"type/constraints as removed field {old_name!r} - if this is actually a "
                    f"rename (not two unrelated fields), the generated AddField/RemoveField "
                    f"pair will DROP {old_name!r}'s data instead of carrying it over. Replace "
                    f"them by hand with RenameField(old_name={old_name!r}, new_name={name!r}, "
                    "field=...) if so."
                )
