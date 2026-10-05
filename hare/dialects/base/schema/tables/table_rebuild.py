from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from hare.ddl.conditions.constraint_condition import ConstraintCondition
from hare.ddl.conditions.exclusive_arc_condition import ExclusiveArcCondition
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.generated_names import GeneratedNames
from hare.ddl.indexes.index import Index
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.fields.field import Field
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.declarations import BackwardOneToOneRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models import Model
from hare.models.enums import ModelOption
from hare.query.expressions import Q


class TableRebuild(SchemaEditorPart):
    """A table rebuilt into a new one - for a change the database can't alter in place: the new table's
    columns mapped from the old ones, the rows copied over with their values converted, the
    constraints and indexes made again."""

    __slots__ = ()

    @staticmethod
    def get_remake_value_conversion_sql(old_field: Field[Any], new_field: Field[Any], quoted_column: str) -> str:
        """The expression copying a column's values into a rebuilt table whose field changed type.

        Args:
            old_field: The field's previous definition.
            new_field: The field's new definition.
            quoted_column: The quoted column of the table being replaced.

        Returns:
            The column itself - the database converts a stored value to the new column type.
        """
        return quoted_column

    async def replace_table(
        self, table_name: str, rebuilt_table_name: str, schema: str | None, fields: Iterable[Field[Any]]
    ) -> None:
        """Drops a table and renames its rebuilt copy into its place.

        Args:
            table_name: The table being replaced.
            rebuilt_table_name: The already filled copy taking its place.
            schema: The tables' schema.
            fields: The fields of the rebuilt table's columns.
        """
        qualified_table = self.editor.qualify_table_name(table_name, schema)
        await self.editor.run_sql(f"DROP TABLE {qualified_table}")
        rebuilt_table = self.editor.qualify_table_name(rebuilt_table_name, schema)
        await self.editor.run_sql(
            self.editor.RENAME_TABLE_TEMPLATE.format(old_table=rebuilt_table, new_table=self.editor.quote(table_name))
        )

    @staticmethod
    def get_remake_column_name(field: Field[Any]) -> str:
        """The DB column a field occupies.

        Args:
            field: A field of a rendered model.

        Returns:
            The column name - for a single-column FK/O2O its key column, since the relation
            field's own source_field names the shadow attribute rather than the column.
        """
        if isinstance(field, ForeignKeyFieldInstance) and len(field.db_column_names) == 1:
            return field.db_column_names[0]
        return field.source_field or field.model_field_name

    def build_remake_column_mapping(
        self,
        model: type[Model],
        create_field: Field[Any] | None = None,
        delete_field: Field[Any] | None = None,
        alter_fields: list[tuple[Field[Any], Field[Any]]] | None = None,
    ) -> dict[str, str]:
        """Maps each surviving column's NEW db name to the SQL expression that copies (or
        backfills) it from the old table - TableRebuild.remake_table()'s own INSERT...SELECT step reads this
        directly."""
        alter_fields = alter_fields or []
        column_mapping = {}
        for field in model._meta.fields_map.values():
            if isinstance(field, (ManyToManyFieldInstance, BackwardForeignKeyRelation, BackwardOneToOneRelation)):
                continue
            db_field = self.get_remake_column_name(field)
            if self.editor.column_definitions.get_non_pk_generated_field_sql(model, field, db_field) is not None:
                # A generated column takes no value in an INSERT - left out of the copy.
                continue
            column_mapping[db_field] = self.editor.quote(db_field)

        if create_field:
            self.add_created_column_mapping(column_mapping, model, create_field)

        self.add_altered_column_mapping(column_mapping, model, alter_fields)

        if delete_field and not isinstance(delete_field, ManyToManyFieldInstance):
            if isinstance(delete_field, ForeignKeyFieldInstance) and len(delete_field.source_fields) > 1:
                for db_field in delete_field.source_fields:
                    column_mapping.pop(db_field, None)
            else:
                column_mapping.pop(self.get_remake_column_name(delete_field), None)

        return column_mapping

    def get_remake_fields_by_db_column(
        self,
        model: type[Model],
        create_field: Field[Any] | None = None,
        delete_field: Field[Any] | None = None,
        alter_fields: list[tuple[Field[Any], Field[Any]]] | None = None,
    ) -> dict[str, Field[Any]]:
        """Maps each surviving column's NEW db name to the field object describing its NEW
        definition (post add/alter/delete) - what TableRebuild.build_remake_field_definitions() below
        actually renders a column definition from."""
        alter_fields = alter_fields or []
        fields_by_db_column = {}
        if isinstance(delete_field, ForeignKeyFieldInstance):
            # The relation field being deleted is no column itself - its key fields are fields of
            # their own.
            delete_shadow_field_names = (
                set(delete_field.source_fields)
                if len(delete_field.source_fields) > 1
                else {delete_field.source_field or delete_field.model_field_name}
            )
        else:
            delete_shadow_field_names = set()
        for field in model._meta.fields_map.values():
            if isinstance(field, (ManyToManyFieldInstance, BackwardForeignKeyRelation, BackwardOneToOneRelation)):
                continue
            if not hasattr(field, "get_column_type"):
                continue
            if isinstance(field, ForeignKeyFieldInstance) and len(field.source_fields) > 1:
                # A relation to a composite key is no column - its key columns are rendered below,
                # its constraint once per relation.
                continue
            if delete_field and field.model_field_name == delete_field.model_field_name:
                continue
            if field.model_field_name in delete_shadow_field_names:
                continue

            actual_field = field
            for old_f, new_f in alter_fields:
                if field.model_field_name == old_f.model_field_name:
                    actual_field = new_f
                    break

            if create_field and field.model_field_name == create_field.model_field_name:
                actual_field = create_field

            db_field = self.get_remake_column_name(actual_field)
            if db_field in fields_by_db_column:
                continue
            fields_by_db_column[db_field] = actual_field
        return fields_by_db_column

    def is_deleted_constraint(self, constraint: Any, delete_constraint: Any) -> bool:
        """True if a Meta.constraints entry is the one being dropped by this rebuild.

        Matches by name when either side has one; a nameless UniqueConstraint falls back to a
        fields match.
        """
        if isinstance(delete_constraint, CheckConstraint):
            return isinstance(constraint, CheckConstraint) and constraint.name == delete_constraint.name
        if isinstance(constraint, UniqueConstraint):
            if delete_constraint.name or constraint.name:
                return constraint.name == delete_constraint.name
            return tuple(constraint.fields) == tuple(delete_constraint.fields)
        return False

    @staticmethod
    def constraint_references_field(constraint: Any, field: Field[Any]) -> bool:
        """True if a Meta.constraints entry names ``field`` among its own fields - a
        UniqueConstraint by its fields, a CheckConstraint whose condition is a Q by the fields
        the condition reads. A CheckConstraint of raw SQL (``RawSQLTerm``) can't be read for field names."""
        if isinstance(constraint, CheckConstraint) and isinstance(constraint.check, (Q, ExclusiveArcCondition)):
            return field.model_field_name in constraint.check.get_referenced_field_names()
        return isinstance(constraint, UniqueConstraint) and field.model_field_name in constraint.fields

    @staticmethod
    def index_references_field(index: Index, field: Field[Any]) -> bool:
        """True if a Meta.indexes entry keys on ``field`` - by its name, or for a relation by one of
        its key columns' fields. An expression index can't be read for field names.

        Args:
            index: The index.
            field: The field.

        Returns:
            Whether the index lists the field.
        """
        field_names = {field.model_field_name, *getattr(field, "source_fields", ())}
        return any(name.removeprefix("-") in field_names for name in index.fields)

    def build_remake_field_definitions(
        self,
        model: type[Model],
        fields_by_db_column: dict[str, Field[Any]],
        delete_field: Field[Any] | None = None,
        alter_fields: list[tuple[Field[Any], Field[Any]]] | None = None,
        delete_constraint: Any = None,
    ) -> list[str]:
        """Renders each surviving column's definition and the model's constraints - what the rebuilt
        table's ``CREATE TABLE`` holds.
        """
        db_table = model._meta.db_table
        qualified_table = self.editor.qualify_table_name(db_table, model._meta.schema)
        field_definitions = [
            self.get_remake_column_definition(model, qualified_table, db_field, actual_field)
            for db_field, actual_field in fields_by_db_column.items()
        ]

        if model._meta.has_composite_primary_key:
            # A composite primary key's fields don't carry primary_key=True - its PRIMARY KEY
            # constraint is added here.
            composite_pk_columns = [
                model._meta.fields_map[name].source_field or name for name in model._meta.primary_key_attribute_names
            ]
            if composite_pk_sql := self.editor.table_creation.get_composite_pk_constraint_sql(
                model, composite_pk_columns
            ):
                field_definitions.append(composite_pk_sql)

        # The check and unique constraints of Meta.constraints go into the CREATE TABLE.
        field_definitions += self.get_remake_check_constraint_definitions(model, delete_field, delete_constraint)

        field_definitions += self.get_remake_composite_foreign_key_definitions(model, delete_field, alter_fields)
        return field_definitions

    @staticmethod
    def get_added_column_names(create_field: Field[Any] | None) -> list[str]:
        """The columns a rebuild adding ``create_field`` creates.

        Args:
            create_field: The field being added, if any.

        Returns:
            Its key columns for a relation, its own column otherwise.
        """
        if create_field is None or isinstance(
            create_field, (ManyToManyFieldInstance, BackwardForeignKeyRelation, BackwardOneToOneRelation)
        ):
            return []
        if isinstance(create_field, ForeignKeyFieldInstance):
            return list(create_field.db_column_names)
        return [TableRebuild.get_remake_column_name(create_field)]

    def get_remade_unique_constraints(
        self, model: type[Model], delete_field: Field[Any] | None, delete_constraint: Any
    ) -> list[UniqueConstraint]:
        """The unique constraints a table rebuild re-creates as unique indexes once the new table
        is in place.

        Args:
            model: The model the table is rebuilt from.
            delete_field: A field whose column the rebuild drops.
            delete_constraint: A constraint the rebuild drops.

        Returns:
            Every UniqueConstraint of the model the rebuild keeps.
        """
        return [
            constraint
            for constraint in getattr(model._meta, ModelOption.CONSTRAINTS, None) or ()
            if isinstance(constraint, UniqueConstraint)
            and not (delete_constraint is not None and self.is_deleted_constraint(constraint, delete_constraint))
            and not (delete_field is not None and self.constraint_references_field(constraint, delete_field))
        ]

    async def get_index_names(self, table_name: str, schema: str | None) -> set[str] | None:
        """The names of a table's indexes, for a rebuild to re-create the ones it had.

        Args:
            table_name: The table.
            schema: Its schema.

        Returns:
            The names, None when the database can't list them - every index is re-created then.
        """
        return None

    async def copy_table_rows(
        self, model: type[Model], new_table_name: str, old_table_name: str, column_mapping: dict[str, str]
    ) -> None:
        """Copies the rows of a table being rebuilt into its new copy.

        Args:
            model: The model rendered from the state the table is rebuilt to.
            new_table_name: The copy, empty.
            old_table_name: The table holding the rows.
            column_mapping: The SQL each column of the copy takes its value from, by the column's name.
        """
        schema = model._meta.schema
        columns_sql = ", ".join(self.editor.quote(column) for column in column_mapping)
        insert_sql = f"""INSERT INTO {self.editor.qualify_table_name(new_table_name, schema)} ({columns_sql})
                SELECT {", ".join(column_mapping.values())}
                FROM {self.editor.qualify_table_name(old_table_name, schema)}"""  # nosec B608
        await self.editor.run_sql(insert_sql)

    def add_created_column_mapping(
        self, column_mapping: dict[str, str], model: type[Model], create_field: Field[Any]
    ) -> None:
        """Adds what the column of a field the rebuild adds is filled with - its default, NULL
        without one; a generated column takes no value.

        Args:
            column_mapping: The SQL each column of the copy takes its value from - added to.
            model: The model.
            create_field: The added field.
        """
        if not isinstance(
            create_field,
            (ManyToManyFieldInstance, BackwardForeignKeyRelation, BackwardOneToOneRelation),
        ):
            if isinstance(create_field, ForeignKeyFieldInstance) and len(create_field.source_fields) > 1:
                # Composite target: every key column is new - filled with NULL.
                for db_field in create_field.source_fields:
                    column_mapping[db_field] = "NULL"
            else:
                db_field = self.get_remake_column_name(create_field)
                # A new generated column takes no value either.
                if (
                    self.editor.column_definitions.get_non_pk_generated_field_sql(model, create_field, db_field)
                    is None
                ):
                    default_value = self.editor.column_backfill.field_backfill_sql_literal(create_field, model)
                    column_mapping[db_field] = default_value if default_value is not None else "NULL"

    def add_altered_column_mapping(
        self, column_mapping: dict[str, str], model: type[Model], alter_fields: list[tuple[Field[Any], Field[Any]]]
    ) -> None:
        """Adds what the columns of the fields the rebuild changes are copied as - converted to the
        new type, and given the field's default where a nullable column becomes NOT NULL.

        Args:
            column_mapping: The SQL each column of the copy takes its value from - added to.
            model: The model.
            alter_fields: (old, new) definitions of the changed fields.
        """
        for old_field, new_field in alter_fields:
            old_db_field = self.get_remake_column_name(old_field)
            new_db_field = self.get_remake_column_name(new_field)
            column_mapping.pop(old_db_field, None)

            # An altered generated column stays out of the column list.
            if (
                self.editor.column_definitions.get_non_pk_generated_field_sql(model, new_field, new_db_field)
                is not None
            ):
                continue

            if old_field.null and not new_field.null:
                default_value = self.editor.column_backfill.field_backfill_sql_literal(new_field, model)
            else:
                default_value = None
            copied_value_sql = self.get_remake_value_conversion_sql(
                old_field, new_field, self.editor.quote(old_db_field)
            )
            if default_value is not None:
                column_mapping[new_db_field] = f"COALESCE({copied_value_sql}, {default_value})"
            else:
                column_mapping[new_db_field] = copied_value_sql

    def get_remake_column_definition(
        self, model: type[Model], qualified_table: str, db_field: str, actual_field: Field[Any]
    ) -> str:
        """The definition of one column of the rebuilt table - its comment and default kept, a
        relation's foreign key with it.

        Args:
            model: The model.
            qualified_table: The model's quoted table.
            db_field: The column.
            actual_field: The field describing its new definition.

        Returns:
            The definition.
        """
        # The column's comment is kept.
        comment_sql = (
            self.editor.table_comments.get_column_comment_sql(
                table=qualified_table, column=db_field, comment=actual_field.description
            )
            if actual_field.description
            else ""
        )
        if isinstance(actual_field, ForeignKeyFieldInstance):
            foreign_key_field = actual_field
            field_type = self.editor.column_definitions.get_table_column_type(
                model, foreign_key_field.to_field_instance
            )

            if self.editor.foreign_key_rebuild.creates_foreign_key(foreign_key_field):
                to_field_name = (
                    foreign_key_field.to_field_instance.source_field
                    or foreign_key_field.to_field_instance.model_field_name
                )
                field_def = self.editor.column_definitions.get_field_sql(
                    db_field=db_field,
                    field_type=field_type,
                    nullable=actual_field.null,
                    unique=actual_field.unique and not actual_field.pk,
                    is_pk=actual_field.pk,
                    comment=comment_sql,
                ) + self.editor.column_definitions.get_foreign_key_reference_string(
                    constraint_name=GeneratedNames.get_foreign_key_name(
                        model._meta.db_table,
                        (db_field,),
                        foreign_key_field.related_model._meta.db_table,
                        (to_field_name,),
                    ),
                    db_field=db_field,
                    table=self.editor.qualify_table_name(
                        foreign_key_field.related_model._meta.db_table,
                        foreign_key_field.related_model._meta.schema,
                    ),
                    field=to_field_name,
                    on_delete=foreign_key_field.db_on_delete,
                    comment="",
                )
            else:
                # No database constraint (creates_foreign_key) means no FK constraint at
                # all - a plain column.
                field_def = self.editor.column_definitions.get_field_sql(
                    db_field=db_field,
                    field_type=field_type,
                    nullable=actual_field.null,
                    unique=actual_field.unique and not actual_field.pk,
                    is_pk=actual_field.pk,
                    comment=comment_sql,
                )
        elif actual_field.pk and actual_field.generated:
            generated_sql = actual_field.get_generated_sql(self.editor.client.dialect)
            if generated_sql:
                field_def = self.editor.GENERATED_PK_TEMPLATE.format(
                    field_name=self.editor.quote(db_field),
                    generated_sql=generated_sql,
                    comment=comment_sql,
                )
            else:
                field_def = self.editor.column_definitions.get_field_sql(
                    db_field=db_field,
                    field_type=self.editor.column_definitions.get_table_column_type(model, actual_field),
                    nullable=actual_field.null,
                    unique=False,
                    is_pk=True,
                    comment=comment_sql,
                )
        else:
            generated_field_def = self.editor.column_definitions.get_non_pk_generated_field_sql(
                model, actual_field, db_field, comment_sql
            )
            if generated_field_def:
                field_def = generated_field_def
            else:
                field_def = self.editor.column_definitions.get_field_sql(
                    db_field=db_field,
                    field_type=self.editor.column_definitions.get_table_column_type(model, actual_field),
                    nullable=actual_field.null,
                    unique=actual_field.unique and not actual_field.pk,
                    is_pk=actual_field.pk,
                    comment=comment_sql,
                )

        if actual_field.has_db_default():
            if hasattr(actual_field.db_default, "get_sql"):
                field_def += f" DEFAULT {self.editor.column_definitions.get_db_default_sql(actual_field.db_default)}"
            else:
                db_value = self.editor.client.dialect.types.get_db_value(actual_field, actual_field.db_default, model)
                escaped = self.editor.client.dialect.literals.get_literal_sql(db_value)
                field_def += f" DEFAULT {escaped}"

        return field_def

    def get_remake_check_constraint_definitions(
        self, model: type[Model], delete_field: Field[Any] | None, delete_constraint: Any
    ) -> list[str]:
        """The check constraints of ``Meta.constraints`` in the rebuilt table's CREATE TABLE - a
        unique constraint is a unique index of its own name, created once the table is in place.

        Args:
            model: The model.
            delete_field: A field whose column the rebuild drops.
            delete_constraint: A constraint the rebuild drops.

        Returns:
            The definitions.
        """
        constraint_definitions: list[str] = []
        for constraint in getattr(model._meta, ModelOption.CONSTRAINTS, None) or ():
            if delete_constraint is not None and self.is_deleted_constraint(constraint, delete_constraint):
                continue
            # A constraint on the column this rebuild drops is dropped with it - `model` is rendered
            # from the old state and still has it.
            if delete_field is not None and self.constraint_references_field(constraint, delete_field):
                continue
            if isinstance(constraint, CheckConstraint):
                constraint_definitions.append(
                    self.editor.CHECK_CONSTRAINT_CREATE_TEMPLATE.format(
                        name=self.editor.quote(constraint.name),
                        check=ConstraintCondition.get_sql(constraint.check, model, self.editor.client),
                    )
                )
            elif isinstance(constraint, UniqueConstraint):
                # A unique constraint is a unique index of its own name - TableRebuild.remake_table() creates
                # it once the table is in place, so it keeps its name and can be dropped without
                # another rebuild.
                self.editor.constraint_statements.check_unique_constraint_supported(constraint)
        return constraint_definitions

    def get_remake_composite_foreign_key_definitions(
        self,
        model: type[Model],
        delete_field: Field[Any] | None,
        alter_fields: list[tuple[Field[Any], Field[Any]]] | None,
    ) -> list[str]:
        """The foreign key constraints of the relations to a composite key in the rebuilt table's
        CREATE TABLE.

        Args:
            model: The model.
            delete_field: A field whose column the rebuild drops.
            alter_fields: (old, new) definitions of the fields the rebuild changes.

        Returns:
            The definitions.
        """
        constraint_definitions: list[str] = []
        # Composite relation constraints, one per relation: the altered field's new definition, and
        # none for the removed one.
        alter_by_name: dict[str, Any] = {old.model_field_name: new for old, new in (alter_fields or ())}
        seen_composite_fields: set[str] = set()
        for field in model._meta.fields_map.values():
            if not isinstance(field, ForeignKeyFieldInstance) or len(field.source_fields) <= 1:
                continue
            if delete_field and field.model_field_name == delete_field.model_field_name:
                continue
            actual_field = alter_by_name.get(field.model_field_name, field)
            if actual_field.model_field_name in seen_composite_fields:
                continue
            seen_composite_fields.add(actual_field.model_field_name)
            if not self.editor.foreign_key_rebuild.creates_foreign_key(actual_field):
                continue
            constraint = self.editor.table_creation.get_composite_foreign_key_constraint(model, actual_field)
            constraint_definitions.append(
                self.editor.FOREIGN_KEY_CONSTRAINT_CREATE_TEMPLATE.format(
                    name=self.editor.quote(constraint.name),
                    fields=", ".join(self.editor.quote(field_name) for field_name in constraint.fields),
                    table=constraint.to_table,
                    to_fields=", ".join(self.editor.quote(field_name) for field_name in constraint.to_fields),
                    on_delete=constraint.on_delete,
                )
            )
        return constraint_definitions

    async def recreate_indexes(
        self,
        model: type[Model],
        fields_by_db_column: dict[str, Field[Any]],
        old_index_names: set[str] | None,
        delete_field: Field[Any] | None,
        create_field: Field[Any] | None,
        added_column_name: str | None,
    ) -> None:
        """Creates again the indexes that went with the old table.

        Args:
            model: The model.
            fields_by_db_column: The field describing each column of the rebuilt table.
            old_index_names: The indexes of the old table, None when they aren't known.
            delete_field: A field whose column the rebuild dropped.
            create_field: A field whose column the rebuild added.
            added_column_name: A column the running AddField added just before.
        """
        # So did the indexes of Meta.indexes; an index on the dropped column isn't created again.
        normalized_meta_indexes = [
            index
            for index in (
                entry if isinstance(entry, Index) else Index(fields=tuple(entry)) for entry in model._meta.indexes
            )
            if delete_field is None or not self.index_references_field(index, delete_field)
        ]
        columns_covered_by_meta_indexes = set()
        column_lists_covered_by_meta_indexes = set()
        for index in normalized_meta_indexes:
            columns = self.editor.index_statements.column_names_for_index(model, index)
            column_lists_covered_by_meta_indexes.add(tuple(columns))
            if len(columns) == 1:
                columns_covered_by_meta_indexes.add(columns[0])

        # And the index of each surviving field's index=True. A column the running AddField added
        # gets its index from the AddIndex that follows.
        added_column_names = set(self.get_added_column_names(create_field))
        if added_column_name is not None:
            added_column_names.add(added_column_name)
        for db_field, actual_field in fields_by_db_column.items():
            field_index = Index(fields=(actual_field.model_field_name,))
            if (
                actual_field.index
                and not actual_field.pk
                and db_field not in columns_covered_by_meta_indexes
                and db_field not in added_column_names
                and (
                    old_index_names is None
                    or self.editor.index_statements.index_name_for_model(model, field_index) in old_index_names
                )
            ):
                await self.editor.add_index(model, field_index)
        # A relation to a composite primary key has one index over all of its key columns.
        for relation_field_name, column_names in model._meta.get_field_index_columns():
            if (
                len(column_names) > 1
                and column_names not in column_lists_covered_by_meta_indexes
                and all(column_name in fields_by_db_column for column_name in column_names)
                and not added_column_names.intersection(column_names)
            ):
                await self.editor.add_index(model, Index(fields=(relation_field_name,)))

        for index in normalized_meta_indexes:
            await self.editor.add_index(model, index)

    async def remake_table(
        self,
        model: type[Model],
        create_field: Field[Any] | None = None,
        delete_field: Field[Any] | None = None,
        alter_fields: list[tuple[Field[Any], Field[Any]]] | None = None,
        delete_constraint: Any = None,
        added_column_name: str | None = None,
    ) -> None:
        """Rebuilds a table from the model: creates the new table under a temporary name, copies
        the rows into it, puts it in place of the old one, then re-creates its triggers, indexes
        and unique constraints, which went with the old table. The alteration strategy of a
        database whose ALTER TABLE can't make the change in place (SQLite's recommended one).

        Args:
            model: The model rendered from the state the table is rebuilt to.
            create_field: A field whose column the rebuild adds.
            delete_field: A field whose column the rebuild drops.
            alter_fields: (old, new) definitions of the fields the rebuild changes.
            delete_constraint: A constraint the rebuild drops.
            added_column_name: A column the running AddField added just before - like
                ``create_field``'s, its own index comes from the AddIndex that follows.
        """
        alter_fields = alter_fields or []
        table_options = model._meta.get_table_options(self.editor.client.dialect)
        if table_options is not None:
            table_options.raise_if_unsupported(model, self.editor.client.features)
        # The table storing the rows is the one rebuilt.
        db_table = (
            table_options.get_storage_table_name(model._meta.db_table) if table_options else model._meta.db_table
        )
        new_table_name = f"new__{db_table}"

        column_mapping = self.build_remake_column_mapping(model, create_field, delete_field, alter_fields)
        fields_by_db_column = self.get_remake_fields_by_db_column(model, create_field, delete_field, alter_fields)
        field_definitions = self.build_remake_field_definitions(
            model, fields_by_db_column, delete_field, alter_fields, delete_constraint
        )

        qualified_new = self.editor.qualify_table_name(new_table_name, model._meta.schema)
        # A field's own index is re-created only when the old table had it - one the running
        # migration removed comes back through the AddIndex that follows, if any.
        old_index_names = await self.get_index_names(db_table, model._meta.schema)
        create_sql = (
            f"CREATE {table_options.get_create_prefix_sql() if table_options else ''}TABLE {qualified_new} "
            f"({', '.join(field_definitions)})"
            f"{table_options.get_create_suffix_sql(model, self.editor.quote) if table_options else ''}"
        )
        await self.editor.run_sql(create_sql)
        if table_options is not None:
            for statement in table_options.get_after_create_sqls(
                model, qualified_new, self.editor.quote, self.editor.client.features
            ):
                await self.editor.run_sql(statement)

        if column_mapping:
            await self.copy_table_rows(model, new_table_name, db_table, column_mapping)

        await self.replace_table(db_table, new_table_name, model._meta.schema, fields_by_db_column.values())
        if table_options is not None:
            for statement in table_options.get_companion_table_sqls(
                model, self.editor.quote, self.editor.client, replaces=True
            ):
                await self.editor.run_sql(statement)

        # Triggers went with the old table - created again.
        for trigger in model._meta.triggers:
            await self.editor.trigger_statements.add_trigger(model, trigger)

        await self.recreate_indexes(
            model, fields_by_db_column, old_index_names, delete_field, create_field, added_column_name
        )

        # A unique constraint is a unique index of its own name, never part of the CREATE TABLE body.
        for constraint in self.get_remade_unique_constraints(model, delete_field, delete_constraint):
            await self.editor.add_constraint(model, constraint)
