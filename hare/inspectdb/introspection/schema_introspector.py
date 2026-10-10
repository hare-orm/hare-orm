from __future__ import annotations

import keyword
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from hare.fields.db_defaults.now import Now
from hare.fields.db_defaults.sql_default import SqlDefault
from hare.inspectdb.constants import (
    CONDITION_AND_SPLIT_RE,
    CONDITION_COLUMN_RE,
    CONDITION_TERM_RE,
    FLOAT_LITERAL_RE,
    INT_LITERAL_RE,
    QUOTED_IDENTIFIER_RE,
    QUOTED_STRING_RE,
    TRIGGER_EVENT_KEYWORD_RE,
    WHITESPACE_RUN_PATTERN,
)
from hare.inspectdb.generation.model_naming import ModelNaming
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.table_info import TableInfo

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.indexes.index import Index
    from hare.ddl.table_options import TableOptions
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.enums import ModelOption
from hare.exceptions import UnSupportedError


class SchemaIntrospector:
    """Reads a database's schema into TableInfo/ColumnInfo/... The entry points take a connection and
    hand the work to its dialect's subclass, which reads the catalog (``fetch_*``) and maps column
    types (``map_column_type``); the shared parsers of defaults, index conditions and triggers live
    here.
    """

    #: The default expressions meaning "the current time", as the database echoes them back -
    #: reconstructed as ``Now()``. Compared case-insensitively.
    NOW_EXPRESSIONS: ClassVar[tuple[str, ...]] = ("CURRENT_TIMESTAMP",)
    #: How a default drawing the next value of a sequence starts - an auto-increment key's, already
    #: implied by ``generated=True``. Compared case-insensitively.
    SEQUENCE_DEFAULT_PREFIXES: ClassVar[tuple[str, ...]] = ()
    #: The type casts (``split_type_cast()``) whose quoted default literal is a number.
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
    def split_type_cast(cls, sql: str) -> tuple[str, str | None]:
        """Splits the type cast the database writes after an expression it echoes back off it.

        Args:
            sql: The expression.

        Returns:
            The expression without the cast and the cast's type - the expression unchanged and
            None by default.
        """
        return sql, None

    @classmethod
    def strip_type_casts(cls, sql: str) -> str:
        """Leaves out every type cast the database adds to an expression it re-serializes.

        Args:
            sql: The expression.

        Returns:
            The expression without casts - unchanged by default.
        """
        return sql

    @classmethod
    def parse_db_default(cls, raw_sql: str) -> Any:
        """Rebuilds a database default from its SQL text: a literal (its type cast split off), the
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
        if sql.lower() in {"true", "false"}:
            return sql.lower() == "true"
        if INT_LITERAL_RE.match(sql):
            return int(sql)
        if FLOAT_LITERAL_RE.match(sql):
            return float(sql)
        literal_sql, cast_type = cls.split_type_cast(sql)
        quoted_match = QUOTED_STRING_RE.match(literal_sql)
        if quoted_match:
            value = quoted_match.group(1).replace("''", "'")
            # A database may echo a numeric default as a quoted literal cast to a number type.
            if cast_type in cls.NUMERIC_CAST_TYPES:
                if INT_LITERAL_RE.match(value):
                    return int(value)
                if FLOAT_LITERAL_RE.match(value):
                    return float(value)
            return value
        return SqlDefault(sql)

    @classmethod
    def get_predicate_equalities(cls, predicate_sql: str) -> dict[str, Any] | None:
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
            term = term.removeprefix("(")
            term = term.removesuffix(")")
            term_match = CONDITION_TERM_RE.match(term)
            if not term_match:
                return None
            column_sql, _ = cls.split_type_cast(term_match.group("column_sql").strip())
            column_match = CONDITION_COLUMN_RE.match(column_sql)
            if not column_match:
                return None
            quoted_column = column_match.group("quoted_column")
            column = (
                quoted_column.replace('""', '"') if quoted_column is not None else column_match.group("bare_column")
            )
            value = cls.parse_db_default(term_match.group("value"))
            if value is None or isinstance(value, SqlDefault):
                return None
            condition[column] = value
        return condition or None

    @staticmethod
    def match_composite_foreign_key_naming(
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
            primary_key_attribute_name = ModelNaming.get_safe_identifier(pk_column, digit_prefix="field_")
            suffix = f"_{primary_key_attribute_name}"
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
            inner_sql = SchemaIntrospector.extract_balanced_parens(stripped_sql, 0)
            if inner_sql is not None and len(inner_sql) == len(stripped_sql) - 2:
                return inner_sql.strip()
        return stripped_sql

    @staticmethod
    def extract_balanced_parens(text: str, open_paren_index: int) -> str | None:
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
    def normalize_event_clause(raw: str) -> str:
        """Collapses whitespace and uppercases the keywords of an ``on`` event clause - column names
        keep their spelling.
        """
        collapsed = WHITESPACE_RUN_PATTERN.sub(" ", raw.strip())
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

    @classmethod
    async def fetch_declared_schema_objects(
        cls,
        connection: DatabaseClient,
        schema: str,
        declared: Mapping[ModelOption, tuple[Any, ...]],
        table_info: TableInfo,
        column_to_field_name: Mapping[str, str],
    ) -> Mapping[ModelOption, tuple[Any, ...]]:
        """Returns the objects a model declares beside its table - its views, materialized views and
        dictionaries - as the database has them: ``hare drift`` compares these. An object the database
        lacks is left out, one defined another way comes as the database defines it, one meaning the
        same as declared stays as declared.

        Args:
            connection: The connection.
            schema: The schema of the model.
            declared: The objects the model declares, by their ``Meta`` option.
            table_info: The model's introspected table.
            column_to_field_name: Column name -> the name of the model field owning it.

        Returns:
            The objects, by their option - as declared by default: the database isn't asked.
        """
        return declared

    @classmethod
    async def fetch_declared_table_options(
        cls,
        connection: DatabaseClient,
        observed: TableOptions | None,
        declared: TableOptions | None,
        table_info: TableInfo,
        column_to_field_name: Mapping[str, str],
    ) -> TableOptions | None:
        """Returns a table's observed options as the model declares them when both mean the same
        storage - ``hare drift`` compares these, so a declaration the database records another way
        isn't reported as a change.

        Args:
            connection: The connection the table was read on.
            observed: The options read from the database (``TableInfo.table_options``).
            declared: The model's ``Meta.table_options`` entry of the dialect, if any.
            table_info: The introspected table.
            column_to_field_name: Column name -> the name of the model field owning it.

        Returns:
            ``observed`` naming fields where it names columns, by default.
        """
        return observed.with_field_names(column_to_field_name) if observed is not None else None

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
    async def fetch_schema_names(cls, connection: DatabaseClient) -> list[str]:
        """Reads the names of the database's schemas.

        Args:
            connection: The connection.

        Returns:
            The names, sorted.

        Raises:
            UnSupportedError: The database has no schemas.
        """
        raise UnSupportedError(f"The {connection.dialect} dialect has no schemas to list")

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
    def reports_nullability(cls, column: ColumnInfo) -> bool:
        """Whether a column's type says if it holds NULL - a type that can't be nullable (a
        container, a NULL written as an empty value) says nothing of the field's ``null``.

        Args:
            column: The introspected column.

        Returns:
            True by default.
        """
        return True

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
