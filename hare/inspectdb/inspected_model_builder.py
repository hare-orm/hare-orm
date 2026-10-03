"""Turns a TableInfo into an InspectedModel - the model state reconstructing the table: fields,
composite primary key and Meta options as real objects. The model source writer and the model
factory both build from it.
"""

from __future__ import annotations

import json
from typing import Any

from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.ddl.indexes.index import Index
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.exceptions import ConfigurationError
from hare.fields.base.field import Field
from hare.fields.enums import OnDelete
from hare.inspectdb.constants import (
    AMBIGUOUS_REASON_SENTINEL_KWARG,
    BASE_FIELD_SENTINEL_KWARG,
    JSON_FIELD_PATH,
)
from hare.inspectdb.inspected_model import InspectedModel
from hare.inspectdb.introspector.schema_introspector import SchemaIntrospector
from hare.inspectdb.naming import ModelNaming
from hare.inspectdb.type_mapping import ColumnTypeMapper
from hare.inspectdb.types.column_info import ColumnInfo
from hare.inspectdb.types.index_info import IndexInfo
from hare.inspectdb.types.table_info import TableInfo
from hare.migrations.state.project.model_state import ModelState
from hare.migrations.writer.migration_writer import MigrationWriter
from hare.models import Model
from hare.models.enums import ModelOption
from hare.models.meta_class import ModelMeta


class InspectedModelBuilder:
    """Builds one table's ``InspectedModel`` - its model state of real field, index and constraint
    objects - across several phases, one method per phase."""

    def __init__(
        self,
        table: TableInfo,
        dialect: str,
        app_label: str = "models",
        fk_target_overrides: dict[str, str] | None = None,
        skip_m2m_through_tables: bool = True,
        class_name: str | None = None,
        extra_reserved_attr_names: frozenset[str] = frozenset(),
    ) -> None:
        self.table = table
        self.dialect = dialect
        #: The dialect's introspector - its index classes and the index names it makes up.
        self.introspector_class = SchemaIntrospector.get_dialect_introspector_class(dialect)
        self.app_label = app_label
        self.fk_target_overrides = fk_target_overrides or {}
        self.skip_m2m_through_tables = skip_m2m_through_tables
        self.class_name = class_name or ModelNaming.get_class_name(table.name)
        #: The fields by attribute name, in column order.
        self.fields: dict[str, Field[Any]] = {}
        #: Why a field is a best-effort reconstruction, by attribute name.
        self.todo_reasons: dict[str, str] = {}
        #: The Meta options, and the Meta body in order - an option, or a comment line.
        self.meta_options: dict[str, Any] = {}
        self.meta_layout: list[ModelOption | str] = []
        #: The attribute names of a composite primary key.
        self.composite_pk_names: list[str] | None = None
        # A composite key's fields aren't marked primary_key=True, and a key column that is a
        # foreign key is renamed ("event_id" -> "event") - CompositePrimaryKey names the attributes.
        self.field_attr_names: dict[str, str] = {}
        #: Attributes whose field class can't be indexed (JSONField, BinaryField, ...) - an index
        #: or UniqueConstraint over one can't be declared on the model.
        self.non_indexable_attr_names: set[str] = set()
        self.reserved_attr_names = ModelMeta.get_reserved_field_names() | extra_reserved_attr_names
        self.used_attr_names: set[str] = {
            name
            for composite_fk in table.composite_foreign_keys
            for name in (composite_fk.field_name, *composite_fk.columns)
        }
        #: Indexes reconstructed as a composite ForeignKeyField's own default index instead of a
        #: Meta.indexes entry.
        self.relation_index_ids: set[int] = set()
        #: A relation can only name a model of this same run, so a foreign key to a table of
        #: another schema stays a plain column.
        self.foreign_keys = {
            column_name: foreign_key
            for column_name, foreign_key in table.foreign_keys.items()
            if foreign_key.target_schema in (None, table.schema)
        }
        self.cross_schema_foreign_keys = {
            column_name: foreign_key
            for column_name, foreign_key in table.foreign_keys.items()
            if column_name not in self.foreign_keys
        }
        #: A DEFERRABLE single-column UNIQUE is declared as a UniqueConstraint - unique=True can't
        #: carry the deferral.
        self.deferrable_unique_column_indexes = {
            index.columns[0]: index for index in table.column_indexes if index.is_unique and index.deferrable
        }
        #: A plain single-column index under a name of its own is declared as a named Meta.indexes
        #: entry - db_index=True always gets a generated name.
        self.named_column_indexes = {
            index.columns[0]: index
            for index in table.column_indexes
            if not index.is_unique
            and index.columns[0] not in self.foreign_keys
            and not self.is_default_index_name(index.name, GeneratedNamePrefix.INDEX, index.columns)
        }

    @staticmethod
    def has_unique_constraint_options(index: IndexInfo) -> bool:
        """Whether a unique index has what only a UniqueConstraint declares - a deferral, non-key
        columns or ``NULLS NOT DISTINCT``."""
        return bool(index.deferrable or index.include or index.nulls_not_distinct)

    def is_default_index_name(
        self, name: str, prefix: GeneratedNamePrefix, column_names: list[str], name_parts: tuple[str, ...] = ()
    ) -> bool:
        """Whether an index's name is the one it gets without an explicit name.

        Args:
            name: The index's name in the database.
            prefix: hare's generated-name prefix for the declaration.
            column_names: The index's columns, or its rendered expression terms.
            name_parts: What else hare hashes into the generated name.

        Returns:
            True for hare's generated name, or a name the database made up for an index or
            UNIQUE constraint declared without one (``SchemaIntrospector.is_unnamed_index_name``).
        """
        if self.introspector_class.is_unnamed_index_name(name, self.table.name, column_names):
            return True
        return name == GeneratedNames.get_index_name(prefix, self.table.name, column_names, name_parts)

    def is_default_special_index_name(
        self, index: IndexInfo, index_class: type[Index], args: list[Any], kwargs: dict[str, Any]
    ) -> bool:
        """Whether a Meta.indexes entry's database name is the one hare generates for it.

        Args:
            index: The introspected index.
            index_class: The reconstructed index's class.
            args: Its positional arguments.
            kwargs: Its keyword arguments.

        Returns:
            True when the declaration without name= gets exactly that name, or when the name it
            would get can't be worked out.
        """
        kwargs = dict(kwargs)
        args = list(args)
        if "fields" in kwargs:
            kwargs["fields"] = index.get_declared_fields(list(index.columns))
        elif index.expression_terms is None and index.has_explicit_null_placement():
            args = index.get_ordered_key_terms(list(index.columns))
        try:
            declared_index = index_class(*args, **kwargs)
            field_names = declared_index.field_names
        except ConfigurationError, TypeError, ValueError:
            return True
        prefix = GeneratedNamePrefix.UNIQUE_INDEX if declared_index.unique else GeneratedNamePrefix.INDEX
        return self.is_default_index_name(index.name, prefix, field_names, declared_index.get_name_parts())

    def is_indexed_by_leading_columns(self, column_names: list[str]) -> bool:
        """Whether a plain or unique index, or the composite primary key, starts with these
        columns in any order - a ForeignKeyField over them then needs no index of its own.

        Args:
            column_names: The relation's key columns.

        Returns:
            True when such an index exists.
        """
        leading_column_lists = [
            index.columns for index in self.table.indexes if not index.is_special(unique_include_is_constraint=True)
        ]
        if self.is_composite_pk:
            leading_column_lists.append(self.pk_columns)
        return any(
            set(leading_columns[: len(column_names)]) == set(column_names) for leading_columns in leading_column_lists
        )

    def get_foreign_key_index_kwargs(self, column_names: list[str], has_index: bool) -> dict[str, Any]:
        """The ``db_index`` kwarg a ForeignKeyField over these columns needs - it's indexed by default.

        Args:
            column_names: The relation's key columns.
            has_index: Whether the database has the relation's own index.

        Returns:
            ``{"db_index": False}`` when the columns have no index at all, else nothing.
        """
        if has_index or self.is_indexed_by_leading_columns(column_names):
            return {}
        return {"db_index": False}

    @staticmethod
    def get_field_db_default(field_path: str, db_default: Any) -> Any:
        """Adapts an introspected column default to the field class it is declared on.

        Args:
            field_path: Dotted path of the generated field class.
            db_default: The introspected default.

        Returns:
            For a JSONField, a string default parsed as the JSON document it holds (kept as a
            string it would be stored as a JSON string literal); otherwise db_default unchanged.
        """
        if field_path != JSON_FIELD_PATH or not isinstance(db_default, str):
            return db_default
        try:
            return json.loads(db_default)
        except ValueError:
            return db_default

    @staticmethod
    def construct(path: str, args: list[Any] | None = None, kwargs: dict[str, Any] | None = None) -> Any:
        """The object ``path(*args, **kwargs)`` builds - a field, an index, a constraint.

        Args:
            path: The dotted path of the class.
            args: The positional arguments.
            kwargs: The keyword arguments.

        Returns:
            The object.
        """
        return MigrationWriter.get_callable(path)(*(args or []), **(kwargs or {}))

    def add_field(self, attr_name: str, field: Field[Any], todo_reason: str | None) -> None:
        """Adds a field, with why it is a best-effort reconstruction when it is one."""
        self.fields[attr_name] = field
        if todo_reason:
            self.todo_reasons[attr_name] = todo_reason

    def add_meta_option(self, option: ModelOption, value: Any) -> None:
        """Adds a Meta option, after the Meta body written so far."""
        self.meta_options[option] = value
        self.meta_layout.append(option)

    def get_fk_target_reference(self, target_table: str) -> str:
        return (
            self.fk_target_overrides.get(target_table)
            or f"{self.app_label}.{ModelNaming.get_class_name(target_table)}"
        )

    def build(self) -> InspectedModel:
        """Builds the inspected model.

        Returns:
            The inspected model - without a state, with ``skipped_note``, when the table was
            skipped as a ManyToManyField through table.
        """
        # The default table name is the lower-cased class name, which differs from a name with
        # underscores.
        self.needs_table_meta = self.table.name != self.class_name.lower()
        # A model with no explicit Meta.schema lives in the connection's default schema.
        self.needs_schema_meta = not self.table.is_in_default_schema

        m2m_through_table_note = self._check_m2m_through_table()
        if m2m_through_table_note is not None and self.skip_m2m_through_tables:
            return InspectedModel(
                class_name=self.class_name, table_name=self.table.name, state=None, skipped_note=m2m_through_table_note
            )

        self._get_primary_key_columns()
        self._detect_relations_needing_related_name()
        fk_columns_needing_raw_name = self._detect_fk_columns_needing_raw_name()
        composite_fk_member_columns = self._track_composite_fk_member_columns()
        self._build_columns(fk_columns_needing_raw_name, composite_fk_member_columns)
        self._build_composite_fk_fields()
        self._build_composite_pk()
        self._build_meta_entries()
        return self.get_inspected_model()

    def get_inspected_model(self) -> InspectedModel:
        """The inspected model of the phases built.

        Returns:
            The inspected model.
        """
        pk_field_name: str | tuple[str, ...]
        if self.composite_pk_names is not None:
            pk_field_name = tuple(self.composite_pk_names)
        else:
            pk_field_name = next((name for name, field in self.fields.items() if field.pk), "")
        state = ModelState(
            name=self.class_name,
            app=self.app_label,
            table=self.table.name,
            abstract=False,
            description=self.table.table_description or "",
            options=dict(self.meta_options),
            bases=(Model,),
            pk_field_name=pk_field_name,
            fields=dict(self.fields),
        )
        return InspectedModel(
            class_name=self.class_name,
            table_name=self.table.name,
            state=state,
            todo_reasons=dict(self.todo_reasons),
            meta_layout=list(self.meta_layout),
        )

    def _check_m2m_through_table(self) -> str | None:
        """A note when the table has hare's many-to-many through table shape - every column in a
        foreign key, two or more of them - which the many-to-many field rebuilds.

        Returns:
            The note, None for any other table.
        """
        self.all_column_names = {column.name for column in self.table.columns}
        if self.table.is_m2m_junction_shape:
            return (
                f"# NOTE: table {self.table.name!r} looks like a ManyToManyField through table "
                "(every column is a foreign key, no other columns) - skipped. Declare the M2M "
                f"field on one of its related models with through={self.table.name!r} instead of "
                "modeling this table directly.\n"
            )
        return None

    def _get_primary_key_columns(self) -> None:
        # Sorted by the constraint's own declared position, not table.columns' physical layout -
        # a composite PK's declared column order can differ from it (see ColumnInfo.pk_position).
        self.pk_columns = [
            column.name
            for column in sorted((c for c in self.table.columns if c.is_pk), key=lambda c: c.pk_position or 0)
        ]
        if not self.pk_columns:
            # No declared PRIMARY KEY - a unique index covering every column in the table is
            # still a genuine multi-column natural key, treated as a composite pk.
            covering_unique_index = next(
                (
                    index
                    for index in self.table.indexes
                    if index.is_unique and len(index.columns) > 1 and set(index.columns) == self.all_column_names
                ),
                None,
            )
            if covering_unique_index is not None:
                self.pk_columns = list(covering_unique_index.columns)
        self.is_composite_pk = len(self.pk_columns) > 1

    def _detect_relations_needing_related_name(self) -> None:
        """Two relations from this table to the same target would both get the default backward
        relation name on that target - every relation to such a target gets an explicit
        ``related_name="<table>_<field>_set"`` instead."""
        # A single-column FK that's also a composite PK member is rendered as a plain field.
        relation_target_tables = [
            fk.target_table
            for column_name, fk in self.foreign_keys.items()
            if not (column_name in self.pk_columns and self.is_composite_pk)
        ] + [cfk.target_table for cfk in self.table.composite_foreign_keys]
        self.targets_needing_related_name = {
            target_table for target_table in relation_target_tables if relation_target_tables.count(target_table) > 1
        }
        self.fk_columns_needing_related_name = {
            column_name
            for column_name, fk in self.foreign_keys.items()
            if fk.target_table in self.targets_needing_related_name
        }

    def _detect_fk_columns_needing_raw_name(self) -> set[str]:
        """The foreign key columns whose name without "_id" collides with another column's - they keep
        the raw name with source_field=.
        """
        candidate_name_columns: dict[str, list[str]] = {}
        for column in self.table.columns:
            candidate = (
                (column.name[:-3] if column.name.endswith("_id") else f"{column.name}_fk")
                if column.name in self.foreign_keys
                else column.name
            )
            candidate_name_columns.setdefault(candidate, []).append(column.name)
        return {
            column_name
            for column_names in candidate_name_columns.values()
            if len(column_names) > 1
            for column_name in column_names
            if column_name in self.foreign_keys
        }

    def _track_composite_fk_member_columns(self) -> set[str]:
        # A composite foreign key's columns are the relation's key columns, not fields of their own.
        composite_fk_member_columns = {
            column_name for cfk in self.table.composite_foreign_keys for column_name in cfk.columns
        }
        for column_name in composite_fk_member_columns:
            self.field_attr_names[column_name] = column_name
        return composite_fk_member_columns

    def _allocate_attr_name(self, preferred_name: str) -> str:
        """Picks a unique attribute name that doesn't shadow a Model attribute.

        Args:
            preferred_name: Already identifier-safe name derived from the column.

        Returns:
            preferred_name, with a trailing underscore while it is reserved and a numeric suffix
            (_2, _3, ...) while it is already taken by an earlier attribute.
        """
        attr_name = preferred_name
        while attr_name in self.reserved_attr_names:
            attr_name = f"{attr_name}_"
        if attr_name in self.used_attr_names:
            suffix = 2
            numbered_name = f"{attr_name}_{suffix}"
            while numbered_name in self.used_attr_names or numbered_name in self.reserved_attr_names:
                suffix += 1
                numbered_name = f"{attr_name}_{suffix}"
            attr_name = numbered_name
        self.used_attr_names.add(attr_name)
        return attr_name

    def _build_columns(self, fk_columns_needing_raw_name: set[str], composite_fk_member_columns: set[str]) -> None:
        for column in self.table.columns:
            if column.name in composite_fk_member_columns:
                continue
            # A single-column FK that's ALSO a composite PK member can't be a ForeignKeyField -
            # CompositePrimaryKey rejects any relation field as a component. It falls through to
            # the plain-field path below instead, with the relation dropped and flagged.
            if column.name in self.foreign_keys and not (column.name in self.pk_columns and self.is_composite_pk):
                self._build_foreign_key_column(column.name, fk_columns_needing_raw_name)
                continue
            self._build_plain_column(column.name)

    def _build_foreign_key_column(self, column_name: str, fk_columns_needing_raw_name: set[str]) -> None:
        column = next(column for column in self.table.columns if column.name == column_name)
        fk = self.foreign_keys[column_name]
        needs_source_field = False
        if column_name in fk_columns_needing_raw_name:
            raw_fk_field_name = column_name
            needs_source_field = True
        else:
            raw_fk_field_name = column_name[:-3] if column_name.endswith("_id") else f"{column_name}_fk"
        fk_field_name = self._allocate_attr_name(
            ModelNaming.get_safe_identifier(raw_fk_field_name, digit_prefix="field_")
        )
        self.field_attr_names[column_name] = fk_field_name
        kwargs: dict[str, Any] = {}
        # A relation's shadow column defaults to "<attribute>_id" - any other real column name
        # (a raw/renamed attribute, or a column without the "_id" suffix) needs source_field=.
        if needs_source_field or f"{fk_field_name}_id" != column_name:
            kwargs["source_field"] = column_name
        if column_name in self.fk_columns_needing_related_name:
            kwargs["related_name"] = ModelNaming.get_safe_identifier(
                f"{self.table.name}_{fk_field_name}_set", digit_prefix="field_"
            )
        # An FK column that is itself the table's (non-composite) primary key holds at most one row
        # per related object - only OneToOneField accepts primary_key=True.
        field_path = "hare.fields.relations.fields.ForeignKeyFieldInstance"
        if column.is_pk and not self.is_composite_pk:
            field_path = "hare.fields.relations.fields.OneToOneFieldInstance"
            kwargs["primary_key"] = True
        else:
            if column.nullable:
                kwargs["null"] = True
            # A single-column UNIQUE FK is a one-to-one relation - the only relation field that
            # emits the UNIQUE constraint.
            if (
                column.is_unique
                and not self.is_composite_pk
                and column_name not in self.deferrable_unique_column_indexes
            ):
                field_path = "hare.fields.relations.fields.OneToOneFieldInstance"
                if column.has_index:
                    kwargs["db_index"] = True
            else:
                kwargs.update(self.get_foreign_key_index_kwargs([column_name], column.has_index))
        if column.db_default is not None:
            kwargs["db_default"] = column.db_default
        if fk.to_field is not None:
            kwargs["to_field"] = fk.to_field
        on_delete, set_default_todo_reason = ColumnTypeMapper.get_set_default_fallback(
            fk.on_delete, column.db_default is not None, column.nullable
        )
        if on_delete != OnDelete.CASCADE:
            kwargs["on_delete"] = on_delete
        if column.description:
            kwargs["description"] = column.description
        field = self.construct(field_path, [self.get_fk_target_reference(fk.target_table)], kwargs)
        self.add_field(fk_field_name, field, set_default_todo_reason)

    def _add_column_options(
        self, kwargs: dict[str, Any], column: ColumnInfo, attr_name: str, is_indexable: bool
    ) -> bool:
        """Adds the options of a field that describe its column: the column name when it differs
        from the attribute, then ``primary_key`` for the model's single-column primary key, else
        ``null`` / ``unique`` / ``db_index`` - a unique or indexed column covered by a deferrable
        constraint or a named index gets that instead.

        Args:
            kwargs: The field's options, added to in order.
            column: The column.
            attr_name: The field's attribute name.
            is_indexable: Whether the field's type can be indexed.

        Returns:
            Whether the column is the model's single-column primary key.
        """
        if attr_name != column.name:
            kwargs["source_field"] = column.name
        if column.is_pk and not self.is_composite_pk:
            kwargs["primary_key"] = True
            return True
        if column.nullable:
            kwargs["null"] = True
        if column.is_unique and is_indexable and column.name not in self.deferrable_unique_column_indexes:
            kwargs["unique"] = True
        if column.has_index and is_indexable and column.name not in self.named_column_indexes:
            kwargs["db_index"] = True
        return False

    def _build_plain_column(self, column_name: str) -> None:
        column = next(column for column in self.table.columns if column.name == column_name)
        attr_name = self._allocate_attr_name(ModelNaming.get_safe_identifier(column.name, digit_prefix="field_"))
        self.field_attr_names[column.name] = attr_name
        path, extra_kwargs, is_ambiguous = ColumnTypeMapper.map_column_type(self.dialect, column)
        ambiguous_reason = extra_kwargs.pop(AMBIGUOUS_REASON_SENTINEL_KWARG, None)
        base_field = extra_kwargs.pop(BASE_FIELD_SENTINEL_KWARG, None)
        dropped_fk_reason = None
        if column.name in self.foreign_keys and column.name in self.pk_columns and self.is_composite_pk:
            target_reference = self.get_fk_target_reference(self.foreign_keys[column.name].target_table)
            dropped_fk_reason = (
                "this column is also a composite primary key component, and hare-orm doesn't "
                f"support a relation field as one - the ForeignKeyField({target_reference}) "
                "relation was dropped; add it back by hand under a different attribute name if needed"
            )
        elif column.name in self.cross_schema_foreign_keys:
            foreign_key = self.cross_schema_foreign_keys[column.name]
            dropped_fk_reason = (
                f"a foreign key to {foreign_key.target_schema}.{foreign_key.target_table}"
                f"({foreign_key.target_column}), a table of another schema than this inspectdb run - the "
                "relation was dropped; declare it by hand against a model of that table with "
                f"Meta.schema = {foreign_key.target_schema!r}"
            )
        is_indexable = ColumnTypeMapper.is_indexable_field_path(path)
        if not is_indexable:
            self.non_indexable_attr_names.add(attr_name)
        dropped_index_reason = None
        if not is_indexable and not column.is_pk and (column.is_unique or column.has_index):
            dropped_index_reason = (
                f"the column has a {'UNIQUE' if column.is_unique else 'plain'} index in the DB, but "
                f"{path.rsplit('.', 1)[1]} can't be indexed - dropped; add it back by hand if it matters"
            )
        identity_reason = None
        if column.identity_generation is not None and not column.is_pk:
            identity_reason = (
                f"GENERATED {column.identity_generation} AS IDENTITY column - generated=True keeps it out of "
                "INSERT/UPDATE and reads the assigned value back, but hare has no identity-column DDL, so "
                "never let a migration create or alter this column"
            )
        todo_reason = (
            ambiguous_reason
            or dropped_fk_reason
            or (f"couldn't confidently map DB type {column.db_type!r}" if is_ambiguous else None)
            or dropped_index_reason
            or identity_reason
        )

        if column.generated_expression is not None:
            # null/unique/db_index/description/source_field belong to the OUTER GeneratedField;
            # only the type-shape kwargs belong on its nested output_field=. db_default is never
            # set - a DB-level DEFAULT alongside GENERATED is rejected by both dialects.
            kwargs: dict[str, Any] = {"expression": column.generated_expression}
            if not column.generated_stored:
                kwargs["stored"] = False
            self._add_column_options(kwargs, column, attr_name, is_indexable)
            if column.description:
                kwargs["description"] = column.description
            kwargs["output_field"] = self.construct(path, kwargs=extra_kwargs)
            self.add_field(
                attr_name, self.construct("hare.fields.generated.GeneratedField", kwargs=kwargs), todo_reason
            )
            return

        kwargs = dict(extra_kwargs)
        is_primary_key = self._add_column_options(kwargs, column, attr_name, is_indexable)
        if not is_primary_key and identity_reason is not None:
            kwargs["generated"] = True
        # A primary key column can carry its own DB-level DEFAULT (e.g. a UUID PK with DEFAULT
        # gen_random_uuid()) - applied regardless of is_pk.
        if column.db_default is not None:
            kwargs["db_default"] = InspectedModelBuilder.get_field_db_default(path, column.db_default)
        if column.description:
            kwargs["description"] = column.description
        if base_field is not None:
            base_field_path, base_field_kwargs = base_field
            kwargs["base_field"] = self.construct(base_field_path, kwargs=base_field_kwargs)
        self.add_field(attr_name, self.construct(path, kwargs=kwargs), todo_reason)

    def _build_composite_fk_fields(self) -> None:
        columns_by_name = {column.name: column for column in self.table.columns}
        for cfk in self.table.composite_foreign_keys:
            kwargs: dict[str, Any] = {}
            # null=/unique=/db_index= apply uniformly to every shadow column of a composite
            # relation - null=True here if ANY member column is nullable (the permissive direction).
            nullable = any(columns_by_name[column_name].nullable for column_name in cfk.columns)
            if nullable:
                kwargs["null"] = True
            # One db_default is copied onto every shadow column of a composite relation, so it can
            # only stand for the members' own DEFAULTs when they all carry the same one.
            column_defaults = [columns_by_name[column_name].db_default for column_name in cfk.columns]
            shared_db_default = column_defaults[0] if len(set(map(repr, column_defaults))) == 1 else None
            if cfk.on_delete == OnDelete.SET_DEFAULT and shared_db_default is not None:
                kwargs["db_default"] = shared_db_default
            relation_index = next(
                (
                    index
                    for index in self.table.indexes
                    if not index.is_unique
                    and not index.is_special(unique_include_is_constraint=True)
                    and index.columns == list(cfk.columns)
                ),
                None,
            )
            if relation_index is not None:
                self.relation_index_ids.add(id(relation_index))
            # A plain UNIQUE over exactly the key columns is a one-to-one relation - the only
            # relation field that emits that constraint, so it isn't declared again in Meta.
            relation_unique_index = next(
                (
                    index
                    for index in self.table.indexes
                    if index.is_unique
                    and not index.deferrable
                    and not index.condition_sql
                    and not index.is_special(unique_include_is_constraint=True)
                    and set(index.columns) == set(cfk.columns)
                ),
                None,
            )
            field_path = "hare.fields.relations.fields.ForeignKeyFieldInstance"
            if relation_unique_index is not None and not self.is_composite_pk:
                self.relation_index_ids.add(id(relation_unique_index))
                field_path = "hare.fields.relations.fields.OneToOneFieldInstance"
                if relation_index is not None:
                    kwargs["db_index"] = True
            else:
                kwargs.update(self.get_foreign_key_index_kwargs(list(cfk.columns), relation_index is not None))
            on_delete, set_default_todo_reason = ColumnTypeMapper.get_set_default_fallback(
                cfk.on_delete, shared_db_default is not None, nullable
            )
            if on_delete != OnDelete.CASCADE:
                kwargs["on_delete"] = on_delete
            if cfk.target_table in self.targets_needing_related_name:
                kwargs["related_name"] = ModelNaming.get_safe_identifier(
                    f"{self.table.name}_{cfk.field_name}_set", digit_prefix="field_"
                )
            field = self.construct(field_path, [self.get_fk_target_reference(cfk.target_table)], kwargs)
            self.add_field(cfk.field_name, field, set_default_todo_reason)

    def _build_composite_pk(self) -> None:
        if not self.is_composite_pk:
            return
        self.composite_pk_names = [self.field_attr_names[column_name] for column_name in self.pk_columns]

    def _get_unique_constraint(self, index: IndexInfo) -> UniqueConstraint:
        """Reconstructs a unique index as a UniqueConstraint - named when its database name isn't
        the generated one, and carrying its predicate and deferral.

        Args:
            index: A plain btree unique index over real columns.

        Returns:
            The UniqueConstraint.
        """
        kwargs: dict[str, Any] = {"fields": [self.field_attr_names[column_name] for column_name in index.columns]}
        if not self.is_default_index_name(index.name, GeneratedNamePrefix.UNIQUE_CONSTRAINT, index.columns):
            kwargs["name"] = index.name
        if index.condition_sql:
            kwargs["condition"] = RawSQLTerm(index.condition_sql)
        if index.deferrable:
            kwargs["deferrable"] = True
        if index.initially_deferred:
            kwargs["initially_deferred"] = True
        if index.include:
            kwargs["include"] = [self.field_attr_names.get(column_name, column_name) for column_name in index.include]
        if index.nulls_not_distinct:
            kwargs["nulls_distinct"] = False
        return UniqueConstraint(**kwargs)

    @staticmethod
    def is_partial_unique_constraint(index: IndexInfo) -> bool:
        """Whether an index is a partial unique btree index over plain columns - the shape a
        UniqueConstraint(condition=...) creates."""
        return bool(
            index.is_unique
            and index.condition_sql
            and not index.index_type
            and not index.opclasses
            and index.expression_terms is None
        )

    def _get_special_index(self, index: IndexInfo) -> Index:
        """Reconstructs an index with an opclass, access method, predicate or expression term.

        Args:
            index: The introspected index.

        Returns:
            The Index, PartialIndex or access-method subclass.
        """
        index_class, index_args, index_kwargs = index.get_index_declaration(
            self.introspector_class.INDEX_CLASSES_BY_TYPE, self.field_attr_names, resolved_keys=False
        )
        if not self.is_default_special_index_name(index, index_class, index_args, index_kwargs):
            index_kwargs["name"] = index.name
        return index_class(*index_args, **index_kwargs)

    def _build_meta_entries(self) -> None:
        table = self.table
        undeclarable_index_comments = []
        declarable_indexes = []
        for index in table.indexes:
            if id(index) in self.relation_index_ids:
                continue
            # Field.indexable only restricts a plain btree index.
            non_indexable_names = [
                self.field_attr_names[column_name]
                for column_name in index.columns
                if not index.index_type and self.field_attr_names.get(column_name) in self.non_indexable_attr_names
            ]
            if non_indexable_names:
                undeclarable_index_comments.append(
                    f"# TODO: {'unique ' if index.is_unique else ''}index {index.name or index.columns!r} wasn't "
                    f"reconstructed - it covers {', '.join(non_indexable_names)}, whose field type can't be "
                    "indexed. Add it back by hand if it matters."
                )
            else:
                declarable_indexes.append(index)
        # A partial unique index under hare's generated PartialIndex(unique=True) name stays that
        # PartialIndex; under any other name it is a UniqueConstraint(condition=...).
        partial_unique_constraint_indexes = [
            index
            for index in declarable_indexes
            if self.is_partial_unique_constraint(index) and self._get_special_index(index).name is not None
        ]
        partial_unique_constraint_index_ids = {id(index) for index in partial_unique_constraint_indexes}
        special_indexes = [
            index
            for index in declarable_indexes
            if index.is_special(unique_include_is_constraint=True)
            and id(index) not in partial_unique_constraint_index_ids
        ]
        plain_indexes = [
            index for index in declarable_indexes if not index.is_special(unique_include_is_constraint=True)
        ]
        multi_column_plain = [index for index in plain_indexes if not index.is_unique]

        if self.needs_table_meta:
            self.add_meta_option(ModelOption.TABLE, table.name)
        if not self.pk_columns:
            # Neither a primary key nor a unique index over every column - a model without one, so
            # the metaclass adds no "id" the table doesn't have.
            self.add_meta_option(ModelOption.PRIMARY_KEY, None)
        if self.needs_schema_meta:
            self.add_meta_option(ModelOption.SCHEMA, table.schema)
        if table.table_description:
            self.add_meta_option(ModelOption.TABLE_DESCRIPTION, table.table_description)
        if table.table_options is not None:
            self.add_meta_option(
                ModelOption.TABLE_OPTIONS, [table.table_options.with_field_names(self.field_attr_names)]
            )

        index_calls: list[Index] = []
        for index in multi_column_plain:
            declared_fields = index.get_declared_fields(
                [self.field_attr_names[column_name] for column_name in index.columns]
            )
            index_kwargs: dict[str, Any] = {"fields": declared_fields}
            name_parts = Index(fields=declared_fields).get_name_parts()
            if not self.is_default_index_name(index.name, GeneratedNamePrefix.INDEX, index.columns, name_parts):
                index_kwargs["name"] = index.name
            index_calls.append(Index(**index_kwargs))
        for column_name, index in self.named_column_indexes.items():
            if self.field_attr_names.get(column_name) in self.non_indexable_attr_names:
                continue
            index_calls.append(Index(fields=[self.field_attr_names[column_name]], name=index.name))
        index_comments: list[str] = []
        for index in special_indexes:
            index_calls.append(self._get_special_index(index))
            index_label = ", ".join(index.expression_terms or index.columns)
            if index.is_unique and index.index_type in self.introspector_class.INDEX_CLASSES_BY_TYPE:
                index_comments.append(
                    f"# NOTE: {index_label} was a UNIQUE index in the DB - Index()/its "
                    "subclasses can't express uniqueness combined with a non-btree access method, "
                    "reconstructed here as non-unique; add the real constraint back by hand if it matters."
                )
            if index.index_type in self.introspector_class.TUNED_INDEX_TYPES and not index.storage_parameters:
                index_comments.append(
                    f"# NOTE: {index_label}'s real tuning parameters (m/ef_construction/lists) "
                    "weren't reported by the database - reconstructed with this index type's own "
                    "defaults; verify/adjust them by hand if the original was tuned differently."
                )

        # A plain unique index stands for the declaration whose name it has: an Index(unique=True),
        # a named UniqueConstraint, or (under the generated name) an unnamed one.
        unique_constraint_calls: list[UniqueConstraint] = []
        unnamed_unique_field_sets: list[tuple[str, ...]] = []
        for index in plain_indexes:
            if not index.is_unique:
                continue
            attribute_names = [self.field_attr_names[column_name] for column_name in index.columns]
            declared_fields = index.get_declared_fields(attribute_names)
            if declared_fields != attribute_names:
                # A descending key - only an Index(unique=True) declares one.
                index_kwargs = {"fields": declared_fields, "unique": True}
                name_parts = Index(fields=declared_fields).get_name_parts()
                if not self.is_default_index_name(
                    index.name, GeneratedNamePrefix.UNIQUE_INDEX, index.columns, name_parts
                ):
                    index_kwargs["name"] = index.name
                index_calls.append(Index(**index_kwargs))
                continue
            has_constraint_options = self.has_unique_constraint_options(index)
            if has_constraint_options or not self.is_default_index_name(
                index.name, GeneratedNamePrefix.UNIQUE_CONSTRAINT, index.columns
            ):
                if not has_constraint_options and self.is_default_index_name(
                    index.name, GeneratedNamePrefix.UNIQUE_INDEX, index.columns
                ):
                    index_calls.append(Index(fields=attribute_names, unique=True))
                else:
                    unique_constraint_calls.append(self._get_unique_constraint(index))
                continue
            # An unnamed constraint carries no name of its own, so two physical unique indexes
            # over the same column set are always redundant.
            if tuple(attribute_names) not in unnamed_unique_field_sets:
                unnamed_unique_field_sets.append(tuple(attribute_names))
                unique_constraint_calls.append(UniqueConstraint(fields=tuple(attribute_names)))
        unique_constraint_calls.extend(
            self._get_unique_constraint(index)
            for index in (*self.deferrable_unique_column_indexes.values(), *partial_unique_constraint_indexes)
        )
        if index_calls:
            self.add_meta_option(ModelOption.INDEXES, index_calls)
        self.meta_layout.extend(index_comments)
        self.meta_layout.extend(undeclarable_index_comments)
        self.meta_layout.extend(
            f"# TODO: index {index.name or index.columns!r} has {', '.join(index.unrepresentable_properties)} in "
            "the database, which no hare index or constraint can declare - reconstructed without it. Keep it "
            "with a RunSQL migration if it matters."
            for index in (*table.column_indexes, *table.indexes)
            if index.unrepresentable_properties
        )
        self.meta_layout.extend(
            f"# TODO: index {name!r} wasn't reconstructed ({raw_def!r}) - an expression term carries "
            "something Index(*expressions)/its subclasses can't represent (an operator class, sort order "
            "or collation next to an expression, or a definition that couldn't be parsed). Add it back by hand."
            for name, raw_def in table.unparsed_indexes
        )
        # Every constraint type shares ONE Meta.constraints list - separate assignments would
        # silently shadow each other.
        constraint_calls: list[Any] = list(unique_constraint_calls)
        for constraint in table.exclusion_constraints:
            renamed_constraint = ExclusionConstraint(
                name=constraint.name,
                expressions=tuple(
                    (
                        self.field_attr_names.get(field_name, field_name)
                        if isinstance(field_name, str)
                        else field_name,
                        operator,
                    )
                    for field_name, operator in constraint.expressions
                ),
                using=constraint.using,
                condition=constraint.condition,
                include=tuple(
                    self.field_attr_names.get(column_name, column_name) for column_name in constraint.include
                ),
                deferrable=constraint.deferrable,
                initially_deferred=constraint.initially_deferred,
            )
            constraint_calls.append(renamed_constraint)
        for check_constraint in table.check_constraints:
            constraint_calls.append(check_constraint)
        if constraint_calls:
            self.add_meta_option(ModelOption.CONSTRAINTS, constraint_calls)
        self.meta_layout.extend(
            f"# TODO: exclusion constraint {name!r} wasn't in a shape ExclusionConstraint(...) "
            f"could reconstruct ({raw_def!r}) - add it back by hand."
            for name, raw_def in table.unparsed_exclusion_constraints
        )
        self.meta_layout.extend(
            f"# TODO: CHECK constraint {name!r} is not validated in the database - the existing rows "
            "haven't been checked against it; a ValidateConstraint migration checks them."
            for name in table.not_valid_constraint_names
        )
        self.meta_layout.extend(
            f"# TODO: CHECK constraint {name!r} wasn't in a shape CheckConstraint(...) could "
            f"reconstruct ({raw_def!r}) - add it back by hand."
            for name, raw_def in table.unparsed_check_constraints
        )
        self.meta_layout.extend(
            f"# TODO: column {name!r} is a generated (GENERATED ALWAYS AS (...)) column, but "
            "its expression text wasn't in a shape this could parse back out of the table's raw SQL "
            f"({raw_sql!r}) - it's been dropped entirely; add it back by hand as a GeneratedField."
            for name, raw_sql in table.unparsed_generated_columns
        )
        if table.triggers:
            self.add_meta_option(ModelOption.TRIGGERS, list(table.triggers))
        self.meta_layout.extend(
            f"# TODO: trigger {name!r} wasn't in a shape Trigger(...) could reconstruct "
            f"({raw_def!r}) - add it back by hand."
            for name, raw_def in table.unparsed_triggers
        )
        self.meta_layout.extend(
            f"# TODO: composite foreign key {name!r} ({', '.join(columns)}) wasn't reconstructed as "
            "a ForeignKeyField - either it doesn't reference the target's whole primary key, or the real "
            'column names don\'t follow the deterministic "<field>_<pk_component>" shadow-column naming '
            "ForeignKeyFieldInstance's composite to_field support requires. The member columns are plain "
            "fields above; add the real relation back by hand."
            for name, columns in table.unparsed_foreign_keys
        )
