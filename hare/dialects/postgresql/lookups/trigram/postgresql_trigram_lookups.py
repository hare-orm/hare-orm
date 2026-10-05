from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Any

from hare.dialects.enums import DialectName
from hare.dialects.postgresql.enums import PostgresqlLookup, PostgresqlTrigramMatching
from hare.dialects.postgresql.lookups.constants import TRIGRAM_EXTENSION
from hare.fields.field import Field
from hare.query.filters.lookups.field_lookup import FieldLookup
from hare.query.filters.lookups.lookups import Lookups
from hare.query.filters.lookups.value_encoders import ValueEncoders
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.term import Term


class PostgresqlTrigramLookups:
    """The ``pg_trgm`` operators of trigram lookups."""

    @staticmethod
    def similar(term: Term, value: str, text_function: Callable[[Term], Term] | None = None) -> BasicCriterion:
        """The text is similar to ``value`` - ``term % value``."""
        return Lookups.regex_criterion(PostgresqlTrigramMatching.SIMILAR, term, value, text_function)

    @staticmethod
    def word_similar(term: Term, value: str, text_function: Callable[[Term], Term] | None = None) -> BasicCriterion:
        """``value`` is similar to a word of the text - ``term %> value``."""
        return Lookups.regex_criterion(PostgresqlTrigramMatching.WORD_SIMILAR, term, value, text_function)

    @staticmethod
    def strict_word_similar(
        term: Term, value: str, text_function: Callable[[Term], Term] | None = None
    ) -> BasicCriterion:
        """``value`` is similar to whole words of the text - ``term %>> value``."""
        return Lookups.regex_criterion(PostgresqlTrigramMatching.STRICT_WORD_SIMILAR, term, value, text_function)

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
