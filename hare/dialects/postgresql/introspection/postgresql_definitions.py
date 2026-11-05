from __future__ import annotations

from typing import Any

from hare.ddl.constants import FUNCTION_LANGUAGE_PATTERN
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.enums import ExclusionConstraintUsing, TriggerForEach, TriggerTiming
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.trigger import Trigger
from hare.dialects.postgresql.constants import (
    EXCLUSION_CONSTRAINT_DEF_RE,
    EXCLUSION_EXPRESSION_RE,
    EXCLUSION_RAW_EXPRESSION_RE,
    POSTGRES_CHECK_CONSTRAINT_DEF_RE,
    POSTGRES_FUNCTION_BODY_WRAPPER_RE,
    POSTGRES_TRIGGERDEF_RE,
    POSTGRES_VARCHAR_IN_LIST_ITEM_RE,
    POSTGRES_VARCHAR_IN_LIST_RE,
    POSTGRESQL_DEFAULT_TRIGGER_LANGUAGE,
)
from hare.inspectdb.introspection.catalog_sql import CatalogSql
from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector
from hare.sql.order import Order


class PostgresqlDefinitions:
    """The definitions PostgreSQL prints of an index's keys, a constraint, a trigger and storage parameters
    (``pg_get_constraintdef()``, ``pg_get_triggerdef()``, ``reloptions``), read back as hare declares them."""

    @staticmethod
    def get_key_orders(row: dict[str, Any]) -> list[str]:
        """Each key's direction and NULL placement - an ``Order`` value - an expression key's too.

        Args:
            row: The index's POSTGRESQL_INDEXES_SQL row.

        Returns:
            The orders, in the keys' order.
        """
        return [
            Order.build(not is_descending, is_nulls_first).value
            for is_descending, is_nulls_first in zip(row["descending_keys"], row["nulls_first_keys"], strict=True)
        ]

    @staticmethod
    def parse_exclusion_constraint(name: str, definition: str) -> ExclusionConstraint | None:
        """Parses ``pg_get_constraintdef()``'s text of an EXCLUDE constraint. A term that isn't a plain
        column becomes a raw SQL term.

        Returns:
            The constraint, None when the text has another shape.
        """
        match = EXCLUSION_CONSTRAINT_DEF_RE.match(definition.strip())
        if not match:
            return None
        using = match.group("using")
        if using not in set(ExclusionConstraintUsing):
            return None
        terms = CatalogSql.split_terms(match.group("expressions"))
        parsed_expressions: list[tuple[str | RawSQLTerm, str]] = []
        for term in terms:
            term_match = EXCLUSION_EXPRESSION_RE.match(term.strip())
            if term_match:
                parsed_expressions.append((term_match.group("field").strip('"'), term_match.group("operator")))
                continue
            raw_match = EXCLUSION_RAW_EXPRESSION_RE.match(term.strip())
            if not raw_match:
                return None
            parsed_expressions.append((RawSQLTerm(raw_match.group("expression")), raw_match.group("operator")))
        if not parsed_expressions:
            return None
        expressions = tuple(parsed_expressions)
        included_columns = []
        if include_sql := match.group("include"):
            for term in CatalogSql.split_terms(include_sql):
                column = CatalogSql.get_identifier(term)
                if column is None:
                    return None
                included_columns.append(column)
        return ExclusionConstraint(
            name=name,
            expressions=expressions,
            using=ExclusionConstraintUsing(using),
            condition=RawSQLTerm(match.group("condition")) if match.group("condition") else None,
            include=tuple(included_columns),
            deferrable=match.group("deferrable") is not None,
            initially_deferred=match.group("initially") == "DEFERRED",
        )

    @staticmethod
    def parse_check_constraint(name: str, definition: str) -> CheckConstraint | None:
        """Best-effort parse of pg_get_constraintdef()'s own canonical text for a CHECK
        constraint. Returns None (surfaced as an unparsed-constraint comment instead) for
        anything not in the "CHECK (...) [NOT VALID]" shape."""
        match = POSTGRES_CHECK_CONSTRAINT_DEF_RE.match(definition.strip())
        if not match:
            return None
        # Postgres wraps the whole predicate in one more pair of parentheses than it was declared
        # with - "CHECK ((age >= 0))" for a declared "age >= 0".
        expression = SchemaIntrospector.strip_outer_parentheses(match.group("expression")) or ""
        return CheckConstraint(
            name=name, check=RawSQLTerm(PostgresqlDefinitions.get_in_list_sql(expression) or expression)
        )

    @staticmethod
    def get_in_list_sql(expression: str) -> str | None:
        """The IN list a CHECK's predicate over a varchar column was declared as - the predicate an enum field's
        CHECK writes: Postgres reads it back as ``(value)::text = ANY (...)``, a text it gives otherwise once
        declared again, so drift would find the declaration of that text another one.

        Args:
            expression: The predicate as Postgres gives it.

        Returns:
            ``column IN ('a', ...)``, None for another predicate.
        """
        match = POSTGRES_VARCHAR_IN_LIST_RE.match(expression)
        if match is None:
            return None
        items = match.group("cast_items") or match.group("items") or ""
        values = [item.group("value") for item in POSTGRES_VARCHAR_IN_LIST_ITEM_RE.finditer(items)]
        if not values or POSTGRES_VARCHAR_IN_LIST_ITEM_RE.sub("", items).replace(", ", ""):
            return None
        return f"{match.group('column')} IN ({', '.join(f"'{value}'" for value in values)})"

    @staticmethod
    def parse_trigger(name: str, trigger_def: str, body: str, language: str, library: str | None) -> Trigger | None:
        """Rebuilds a ``Trigger`` from its canonical definition and catalog function attributes.

        Args:
            name: The trigger name.
            trigger_def: PostgreSQL's canonical trigger definition.
            body: The function source from ``pg_proc.prosrc``.
            language: The function's catalog language name.
            library: A shared library from ``pg_proc.probin``, None for a source body.

        Returns:
            The trigger, including constraint deferrability, or None for an unsupported trigger
            definition, an unrepresentable language name, or a C/shared-library function whose
            symbol is not a source body.
        """
        if language == "c" or library is not None:
            return None
        # Trigger DDL writes LANGUAGE as an unquoted identifier. Keep names requiring quoting
        # diagnostic rather than folding a case-sensitive catalog name or emitting invalid SQL.
        if language != language.lower() or not FUNCTION_LANGUAGE_PATTERN.fullmatch(language):
            return None
        trigger_match = POSTGRES_TRIGGERDEF_RE.match(trigger_def.strip())
        if not trigger_match:
            return None
        if trigger_match.group("from_table") is not None:
            # A constraint trigger's FROM other_table clause has no Trigger field to hold it -
            # reconstructing without it would silently produce a different trigger, not a
            # faithful (if incomplete) one, so this falls back to an unparsed-trigger comment.
            return None
        when = trigger_match.group("when")
        raw_body = body.strip()
        wrapper_match = POSTGRES_FUNCTION_BODY_WRAPPER_RE.match(raw_body)
        body = wrapper_match.group("inner").strip() if wrapper_match else raw_body
        # pg_get_triggerdef() prints NOT DEFERRABLE for a constraint trigger that isn't deferrable.
        is_constraint_trigger = bool(trigger_match.group("constraint"))
        deferrable = is_constraint_trigger and trigger_match.group("not_deferrable") is None
        initially_deferred = deferrable and trigger_match.group("initially") == "DEFERRED"
        return Trigger(
            name=name,
            on=SchemaIntrospector.normalize_event_clause(trigger_match.group("on")),
            body=RawSQLTerm(body),
            # The regex only ever captures one of TriggerTiming's/TriggerForEach's own values, so
            # this conversion can't fail.
            timing=TriggerTiming(trigger_match.group("timing").upper()),
            for_each=TriggerForEach(trigger_match.group("for_each").upper()),
            when=RawSQLTerm(when.strip()) if when else None,
            language=None if language == POSTGRESQL_DEFAULT_TRIGGER_LANGUAGE else language,
            deferrable=deferrable,
            initially_deferred=initially_deferred,
        )

    @staticmethod
    def parse_storage_parameters(reloptions: list[str] | None) -> dict[str, str]:
        """Splits an index's ``pg_class.reloptions`` (``["lists=5", ...]``) into a dict.

        Args:
            reloptions: The raw ``name=value`` entries, or None when the index has none.

        Returns:
            Parameter name -> raw value text.
        """
        storage_parameters: dict[str, str] = {}
        for option in reloptions or ():
            name, _separator, value = option.partition("=")
            storage_parameters[name] = value
        return storage_parameters

    @staticmethod
    def get_storage_parameter_value(text: str) -> bool | int | float | str:
        """Returns a table storage parameter's value as ``PostgresqlTableOptions`` declares it -
        ``pg_class.reloptions`` keeps each one as the text it was set with.

        Args:
            text: The value's text.

        Returns:
            An integer, a float or a boolean when the text spells one, the text otherwise.
        """
        for number_type in (int, float):
            try:
                return number_type(text)
            except ValueError:
                pass
        lowered_text = text.lower()
        if lowered_text in {"true", "on"}:
            return True
        if lowered_text in {"false", "off"}:
            return False
        return text

    @staticmethod
    def get_storage_parameters(reloptions: list[str] | None) -> dict[str, bool | int | float | str]:
        """A table's storage parameters from its ``pg_class.reloptions``.

        Args:
            reloptions: The raw ``name=value`` entries, or None when the table has none.

        Returns:
            Parameter name -> value.
        """
        return {
            name: PostgresqlDefinitions.get_storage_parameter_value(value)
            for name, value in PostgresqlDefinitions.parse_storage_parameters(reloptions).items()
        }
