from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Any

from hare.dialects.enums import DialectName
from hare.dialects.postgresql.constants import TRIGRAM_EXTENSION
from hare.dialects.postgresql.enums import PostgresqlLookup, PostgresqlTrigramMatching
from hare.fields.base.field import Field
from hare.query.filters.encoders import ValueEncoders
from hare.query.filters.field_lookup import FieldLookup
from hare.query.filters.lookups import Lookups
from hare.sql.terms.base.term import Term
from hare.sql.terms.criteria.basic_criterion import BasicCriterion


class PostgresqlTrigramLookups:
    """The ``pg_trgm`` operators of trigram lookups."""

    @staticmethod
    def similar(field: Term, value: str, text_function: Callable[[Term], Term] | None = None) -> BasicCriterion:
        """The text is similar to ``value`` - ``field % value``."""
        return Lookups.regex_criterion(PostgresqlTrigramMatching.SIMILAR, field, value, text_function)

    @staticmethod
    def word_similar(field: Term, value: str, text_function: Callable[[Term], Term] | None = None) -> BasicCriterion:
        """``value`` is similar to a word of the text - ``field %> value``."""
        return Lookups.regex_criterion(PostgresqlTrigramMatching.WORD_SIMILAR, field, value, text_function)

    @staticmethod
    def strict_word_similar(
        field: Term, value: str, text_function: Callable[[Term], Term] | None = None
    ) -> BasicCriterion:
        """``value`` is similar to whole words of the text - ``field %>> value``."""
        return Lookups.regex_criterion(PostgresqlTrigramMatching.STRICT_WORD_SIMILAR, field, value, text_function)

    @staticmethod
    def get_lookup(operator: Callable[..., BasicCriterion], field: Field[Any] | None) -> FieldLookup:
        """One trigram lookup of a field - it matches the value's text.

        Args:
            operator: The lookup's operator.
            field: The field, None for a value with no field.

        Returns:
            The lookup.
        """
        text_function = None if field is None else field.get_like_text_function()
        return FieldLookup(operator, ValueEncoders.encode_string, text_function=text_function)

    @classmethod
    def register(cls) -> None:
        """Adds the trigram lookups to every value: they match its text, whatever its type."""
        for lookup, operator in (
            (PostgresqlLookup.TRIGRAM_SIMILAR, cls.similar),
            (PostgresqlLookup.TRIGRAM_WORD_SIMILAR, cls.word_similar),
            (PostgresqlLookup.TRIGRAM_STRICT_WORD_SIMILAR, cls.strict_word_similar),
        ):
            Field.register_lookup(
                lookup,
                partial(cls.get_lookup, operator),
                value_type=str,
                dialects=(DialectName.POSTGRESQL,),
                required_extension=TRIGRAM_EXTENSION,
            )
