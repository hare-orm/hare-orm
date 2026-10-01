from __future__ import annotations

import keyword
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from hare.fields.db_defaults.now import Now
from hare.fields.db_defaults.sql_default import SqlDefault
from hare.inspectdb.constants import (
    CONDITION_AND_SPLIT_RE,
    CONDITION_TERM_RE,
    FLOAT_LITERAL_RE,
    INT_LITERAL_RE,
    QUOTED_IDENTIFIER_RE,
    QUOTED_STRING_RE,
    TRIGGER_EVENT_KEYWORD_RE,
)
from hare.inspectdb.naming import ModelNaming
from hare.inspectdb.types.column_info import ColumnInfo
from hare.inspectdb.types.table_info import TableInfo

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.indexes.index import Index
    from hare.ddl.table_options import TableOptions
    from hare.dialects.base.client.database_client import DatabaseClient
from hare.inspectdb.exceptions import TableNotFoundError, UnsupportedDialectError


class SchemaIntrospector:
    """Reads a database's schema into TableInfo/ColumnInfo/... The entry points take a connection and
    hand the work to its dialect's subclass, which reads the catalog (``fetch_*``) and maps column
    types (``map_column_type``); the shared parsers of defaults, index conditions and triggers live
    here.
    """

    #: The default expressions meaning "the current time", as the database echoes them back -
    #: reconstructed as ``Now()``. Compared case-insensitively.
    NOW_EXPRESSIONS: ClassVar[tuple[str, ...]] = ("now()", "CURRENT_TIMESTAMP")
    #: How a default drawing the next value of a sequence starts - an auto-increment key's, already
    #: implied by ``generated=True``. Compared case-insensitively.
    SEQUENCE_DEFAULT_PREFIXES: ClassVar[tuple[str, ...]] = ()
    #: The type casts whose quoted default literal is a number (``'-1.5'::double precision``).
    NUMERIC_CAST_TYPES: ClassVar[frozenset[str]] = frozenset()
    #: The ``Index`` subclass reconstructing an index of each access method the dialect's own index
    #: classes cover (``gin``, ``hnsw``, ...) - any other one is a plain ``Index``/``PartialIndex``.
    INDEX_CLASSES_BY_TYPE: ClassVar[Mapping[str, type[Index]]] = {}
    #: The access methods whose ``WITH (...)`` storage parameters are rendered as constructor
    #: kwargs - a NOTE comment flags an index the database reported none for.
    TUNED_INDEX_TYPES: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def is_unnamed_index_name(cls, name: str, table_name: str, column_names: Sequence[str]) -> bool:
        """Whether a name is one the database made up for an index or UNIQUE constraint declared
        without one.

        Args:
            name: The index's name in the database.
            table_name: Its table.
            column_names: Its columns, or its rendered expression terms.

        Returns:
            True for no name at all by default.
        """
        return not name

    @classmethod
    def parse_db_default(cls, raw_sql: str) -> Any:
        """Rebuilds a database default from its SQL text: a literal (a ``::type`` cast stripped), the
        dialect's current time as ``Now()``, anything else as ``SqlDefault``.

        Returns:
            The default, None for a sequence default (implied by ``generated=True``) or a bare NULL.
        """
        sql = raw_sql.strip()
        lower_sql = sql.lower()
        if lower_sql == "null" or lower_sql.startswith(
            tuple(prefix.lower() for prefix in cls.SEQUENCE_DEFAULT_PREFIXES)
        ):
            return None
        if lower_sql in {expression.lower() for expression in cls.NOW_EXPRESSIONS}:
            return Now()
        if sql.lower() in ("true", "false"):
            return sql.lower() == "true"
        if INT_LITERAL_RE.match(sql):
            return int(sql)
        if FLOAT_LITERAL_RE.match(sql):
            return float(sql)
        quoted_match = QUOTED_STRING_RE.match(sql)
        if quoted_match:
            value = quoted_match.group(1).replace("''", "'")
            # Postgres echoes a negative or out-of-int4-range numeric default as a quoted, cast
            # literal (e.g. "'-1.5'::double precision", "'9000000000'::bigint").
            if quoted_match.group("cast_type") in cls.NUMERIC_CAST_TYPES:
                if INT_LITERAL_RE.match(value):
                    return int(value)
                if FLOAT_LITERAL_RE.match(value):
                    return float(value)
            return value
        return SqlDefault(sql)

    @staticmethod
    def get_predicate_equalities(predicate_sql: str) -> dict[str, Any] | None:
        """A partial-index predicate made of ANDed ``column = literal`` equalities, as
        ``{column: value}`` - how a declared predicate and the one the database writes back
        (``"((status = 'active'::text) AND (category = 'x'::text))"``) are compared.

        Args:
            predicate_sql: The predicate.

        Returns:
            The equalities, or None for a predicate of any other shape.
        """
        sql = predicate_sql.strip()
        if sql.startswith("(") and sql.endswith(")"):
            sql = sql[1:-1]
        condition: dict[str, Any] = {}
        for part in CONDITION_AND_SPLIT_RE.split(sql):
            term = part.strip()
            if term.startswith("("):
                term = term[1:]
            if term.endswith(")"):
                term = term[:-1]
            match = CONDITION_TERM_RE.match(term)
            if not match:
                return None
            quoted_column = match.group("quoted_column")
            column = quoted_column.replace('""', '"') if quoted_column is not None else match.group("bare_column")
            value = SchemaIntrospector.parse_db_default(match.group("value"))
            if value is None or isinstance(value, SqlDefault):
                return None
            condition[column] = value
        return condition or None

    @staticmethod
    def _match_composite_foreign_key_naming(
        local_columns: list[str], target_columns: list[str], target_pk_columns: list[str]
    ) -> tuple[str, tuple[str, ...]] | None:
        """Matches a composite foreign key's columns against the ``<field>_<target key part>`` naming
        of a relation's key columns - possible only when it references the target's whole primary
        key and one prefix reproduces every column name.

        Returns:
            The field name and the columns in the target's key order, None when there is no match.
        """
        if len(target_columns) != len(target_pk_columns) or set(target_columns) != set(target_pk_columns):
            return None
        target_to_local = dict(zip(target_columns, local_columns, strict=True))
        ordered_local_columns = [target_to_local[pk_column] for pk_column in target_pk_columns]
        field_name: str | None = None
        for local_column, pk_column in zip(ordered_local_columns, target_pk_columns, strict=True):
            # Assumed: the target's attribute for a key column is its safe identifier.
            pk_attr_name = ModelNaming.get_safe_identifier(pk_column, digit_prefix="field_")
            suffix = f"_{pk_attr_name}"
            if not local_column.endswith(suffix):
                return None
            candidate = local_column[: -len(suffix)]
            if not candidate or not candidate.isidentifier() or keyword.iskeyword(candidate):
                return None
            if field_name is None:
                field_name = candidate
            elif field_name != candidate:
                return None
        if field_name is None:
            return None
        return field_name, tuple(ordered_local_columns)

    @staticmethod
    def strip_outer_parentheses(sql: str | None) -> str | None:
        """Removes one pair of parentheses wrapping a whole SQL expression.

        Args:
            sql: The expression text.

        Returns:
            ``a > 0`` for ``(a > 0)``; the stripped text itself when no single pair wraps all of
            it (``(a) + (b)``); None for None.
        """
        if sql is None:
            return None
        stripped_sql = sql.strip()
        if stripped_sql.startswith("("):
            inner_sql = SchemaIntrospector._extract_balanced_parens(stripped_sql, 0)
            if inner_sql is not None and len(inner_sql) == len(stripped_sql) - 2:
                return inner_sql.strip()
        return stripped_sql

    @staticmethod
    def _extract_balanced_parens(text: str, open_paren_index: int) -> str | None:
        """The text between the ``(`` at ``open_paren_index`` and its matching ``)``, nested
        parentheses included.

        Returns:
            The text, None when the parentheses never balance.
        """
        depth = 0
        for index in range(open_paren_index, len(text)):
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
                if depth == 0:
                    return text[open_paren_index + 1 : index]
        return None

    @staticmethod
    def _normalize_event_clause(raw: str) -> str:
        """Collapses whitespace and uppercases the keywords of an ``on`` event clause - column names
        keep their spelling.
        """
        collapsed = re.sub(r"\s+", " ", raw.strip())
        parts = QUOTED_IDENTIFIER_RE.split(collapsed)
        quoted = QUOTED_IDENTIFIER_RE.findall(collapsed)
        pieces = [SchemaIntrospector._uppercase_event_keywords(parts[0])]
        for quoted_part, next_part in zip(quoted, parts[1:], strict=True):
            pieces.append(quoted_part)
            pieces.append(SchemaIntrospector._uppercase_event_keywords(next_part))
        return "".join(pieces)

    @staticmethod
    def _uppercase_event_keywords(text: str) -> str:
        """Uppercases every trigger event keyword in ``text``, leaving everything else as is."""
        return TRIGGER_EVENT_KEYWORD_RE.sub(lambda match: match.group(0).upper(), text)

    #: The type rules of ``map_column_type()``: a keyword the lower-cased column type contains,
    #: the field class path it maps onto, and that field's default kwargs - the first match wins.
    TYPE_MAP: ClassVar[list[tuple[str, str, dict[str, Any]]]] = []

    @staticmethod
    def split_top_level_terms(expressions_sql: str) -> list[str]:
        """Splits a comma-separated list of SQL terms on top-level commas only - a function call has
        commas of its own.

        Args:
            expressions_sql: The list.

        Returns:
            The terms, unstripped.
        """
        terms = []
        current: list[str] = []
        depth = 0
        for char in expressions_sql:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            if char == "," and depth == 0:
                terms.append("".join(current))
                current = []
            else:
                current.append(char)
        terms.append("".join(current))
        return terms

    @staticmethod
    def quote_identifier(name: str) -> str:
        """Wraps ``name`` in standard SQL double quotes, doubling any embedded one."""
        return '"' + name.replace('"', '""') + '"'

    @staticmethod
    def get_dialect_introspector_class(dialect_name: str) -> type[SchemaIntrospector]:
        """Returns the introspector of a dialect.

        Args:
            dialect_name: The dialect's name.

        Returns:
            The dialect's ``SchemaIntrospector`` subclass.

        Raises:
            UnsupportedDialectError: The dialect has no introspector.
        """
        # Local import: the registry's dialects import this module.
        from hare.dialects.registry import DialectRegistry

        introspector_class = DialectRegistry.get_dialect(dialect_name).introspector_class
        if introspector_class is None:
            raise UnsupportedDialectError(dialect_name)
        return introspector_class

    @staticmethod
    def get_introspector_class(connection: DatabaseClient) -> type[SchemaIntrospector]:
        """Returns the introspector of a connection's dialect.

        Args:
            connection: The connection.

        Returns:
            The dialect's ``SchemaIntrospector`` subclass.

        Raises:
            UnsupportedDialectError: The dialect has no introspector.
        """
        introspector_class = connection.dialect.introspector_class
        if introspector_class is None:
            raise UnsupportedDialectError(connection.dialect.name)
        return introspector_class

    @staticmethod
    async def get_default_schema(connection: DatabaseClient) -> str:
        """The schema an unqualified CREATE TABLE lands in.

        Args:
            connection: The connection.

        Returns:
            The schema's name.

        Raises:
            UnsupportedDialectError: The connection's dialect has no introspector.
        """
        return await SchemaIntrospector.get_introspector_class(connection).fetch_default_schema(connection)

    @classmethod
    def get_declared_table_options(
        cls,
        observed: TableOptions | None,
        declared: TableOptions | None,
        table_info: TableInfo,
        column_to_field_name: Mapping[str, str],
    ) -> TableOptions | None:
        """Returns a table's observed options as the model declares them when both mean the same
        storage - ``hare drift`` compares these, so a declaration the database records another way
        isn't reported as a change.

        Args:
            observed: The options read from the database (``TableInfo.table_options``).
            declared: The model's ``Meta.table_options`` entry of the dialect, if any.
            table_info: The introspected table.
            column_to_field_name: Column name -> the name of the model field owning it.

        Returns:
            ``observed`` naming fields where it names columns, by default.
        """
        return observed.with_field_names(column_to_field_name) if observed is not None else None

    @staticmethod
    async def get_table_names(
        connection: DatabaseClient, schema: str | None = None, *, include_partitions: bool = False
    ) -> list[str]:
        """Lists every table name in the given schema.

        Args:
            connection: The connection.
            schema: The schema to list tables from, the connection's default one (the schema an
                unqualified CREATE TABLE lands in) when None - ignored by a dialect without
                schemas. Every table in another schema is otherwise invisible to inspectdb.
            include_partitions: Whether to list the partitions of a partitioned table too -
                otherwise its partitioned parent table stands for them.

        Returns:
            The table names.

        Raises:
            UnsupportedDialectError: The connection's dialect has no introspector.
            SchemaNotFoundError: The schema doesn't exist.
        """
        introspector_class = SchemaIntrospector.get_introspector_class(connection)
        if schema is None:
            schema = await introspector_class.fetch_default_schema(connection)
        return await introspector_class.fetch_table_names(connection, schema, include_partitions)

    @staticmethod
    async def table_exists(connection: DatabaseClient, table: str) -> bool:
        """Returns whether a table exists in the connection's default schema.

        Args:
            connection: The connection.
            table: The table's name.

        Returns:
            Whether it exists.

        Raises:
            UnsupportedDialectError: The connection's dialect has no introspector.
        """
        return await SchemaIntrospector.get_introspector_class(connection).fetch_table_exists(connection, table)

    @staticmethod
    async def inspect_table(
        connection: DatabaseClient, table: str, schema: str | None = None, *, verify_exists: bool = True
    ) -> TableInfo:
        """Inspects one table's full schema.

        Args:
            connection: The connection.
            table: The table.
            schema: The schema ``table`` lives in, the connection's default one when None -
                ignored by a dialect without schemas.
            verify_exists: See inspect_tables().

        Returns:
            The table's schema.

        Raises:
            UnsupportedDialectError: The connection's dialect has no introspector.
            TableNotFoundError: If ``table`` doesn't exist.
        """
        table_infos = await SchemaIntrospector.inspect_tables(
            connection, [table], schema=schema, verify_exists=verify_exists
        )
        return table_infos[0]

    @staticmethod
    async def inspect_tables(
        connection: DatabaseClient, tables: list[str], schema: str | None = None, *, verify_exists: bool = True
    ) -> list[TableInfo]:
        """Inspects several tables' full schemas.

        Args:
            connection: The connection.
            tables: The tables to inspect.
            schema: The schema the tables live in, the connection's default one when None -
                ignored by a dialect without schemas.
            verify_exists: Set to ``False`` only when the caller already matched ``tables``
                against a table list it just fetched itself - skips this method's own
                get_table_names() round trip. A dialect whose catalog statements take no bind
                parameters relies on that check to make a table name safe in them.

        Returns:
            One TableInfo per entry of ``tables``, in the same order.

        Raises:
            UnsupportedDialectError: The connection's dialect has no introspector.
            TableNotFoundError: If a table doesn't exist - checked explicitly, since a catalog
                that silently returns nothing for one would otherwise describe a table with no
                columns at all.
        """
        introspector_class = SchemaIntrospector.get_introspector_class(connection)
        if schema is None:
            schema = await introspector_class.fetch_default_schema(connection)
        if verify_exists and tables:
            existing_table_names = set(await introspector_class.fetch_table_names(connection, schema, True))
            for table in tables:
                if table not in existing_table_names:
                    raise TableNotFoundError(table)
        return await introspector_class.fetch_tables(connection, tables, schema)

    @classmethod
    async def fetch_default_schema(cls, connection: DatabaseClient) -> str:
        """Reads the schema an unqualified CREATE TABLE lands in - implemented by each dialect.

        Args:
            connection: The connection.

        Returns:
            The schema's name.
        """
        raise NotImplementedError

    @classmethod
    async def fetch_table_names(cls, connection: DatabaseClient, schema: str, include_partitions: bool) -> list[str]:
        """Reads the table names of a schema - implemented by each dialect.

        Args:
            connection: The connection.
            schema: The schema.
            include_partitions: Whether to list the partitions of a partitioned table too.

        Returns:
            The table names.
        """
        raise NotImplementedError

    @classmethod
    async def fetch_table_exists(cls, connection: DatabaseClient, table: str) -> bool:
        """Reads whether a table exists in the connection's default schema - with the dialect's
        table listing by default; a dialect with a one-row catalog lookup overrides it.

        Args:
            connection: The connection.
            table: The table's name.

        Returns:
            Whether it exists.
        """
        default_schema = await cls.fetch_default_schema(connection)
        return table in await cls.fetch_table_names(connection, default_schema, True)

    @classmethod
    async def fetch_tables(cls, connection: DatabaseClient, tables: list[str], schema: str) -> list[TableInfo]:
        """Reads existing tables' schemas - implemented by each dialect.

        Args:
            connection: The connection.
            tables: The tables.
            schema: The schema they live in.

        Returns:
            One TableInfo per entry of ``tables``, in the same order.
        """
        raise NotImplementedError

    @classmethod
    def map_column_type(cls, column: ColumnInfo) -> tuple[str, dict[str, Any], bool]:
        """Maps one column onto a field class path and its type-shape kwargs, by ``TYPE_MAP``.

        Args:
            column: The introspected column.

        Returns:
            (field_path, extra_kwargs, is_ambiguous) - is_ambiguous marks a type with no
            confident match, reconstructed as the most general TextField.
        """
        lowered = column.db_type.lower()
        for type_keyword, path, default_kwargs in cls.TYPE_MAP:
            if type_keyword in lowered:
                kwargs = dict(default_kwargs)
                if path == "hare.fields.data.text.CharField" and column.max_length is not None:
                    kwargs["max_length"] = column.max_length
                elif path == "hare.fields.data.text.CharField" and cls.maps_unbounded_char_to_text():
                    # An unbounded varchar - TextField has no length ceiling either, unlike the
                    # guessed max_length=255 default.
                    return "hare.fields.data.text.TextField", {}, False
                if path == "hare.fields.data.numeric.DecimalField":
                    if column.numeric_precision is not None:
                        kwargs["max_digits"] = column.numeric_precision
                    if column.numeric_scale is not None:
                        kwargs["decimal_places"] = column.numeric_scale
                return path, kwargs, False
        return "hare.fields.data.text.TextField", {}, True

    @classmethod
    def column_types_differ(cls, declared_type: str, column: ColumnInfo) -> bool:
        """Whether an introspected column's type is not the one a field declares - by default never:
        a comparison errs on "equal", and a dialect that can normalize its type spellings says
        otherwise.

        Args:
            declared_type: The field's SQL type.
            column: The introspected column.

        Returns:
            False.
        """
        return False

    @classmethod
    def get_type_mismatch_texts(cls, declared_type: str, column: ColumnInfo) -> tuple[str, str]:
        """Returns how a drift report names a column's type and the one its field declares.

        Args:
            declared_type: The field's SQL type.
            column: The introspected column.

        Returns:
            The observed and the expected type.
        """
        return column.full_type or column.db_type, declared_type

    @classmethod
    def maps_unbounded_char_to_text(cls) -> bool:
        """Whether a character column without a length (an unbounded ``varchar``) is a TextField
        rather than a CharField of a guessed length.

        Returns:
            False by default.
        """
        return False
