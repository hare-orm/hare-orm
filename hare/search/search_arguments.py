from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hare.query.expressions import Expression, ExpressionContext, ExpressionResult, Value
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.sql.terms.term import Term


class SearchArguments:
    """The arguments of the full-text search expressions - a field name, a value or an expression."""

    @staticmethod
    def get_result(
        owner: Any,
        attribute: str,
        expression_context: ExpressionContext,
        *,
        treat_str_as_field: bool,
        index: int | None = None,
        binds_whole: bool = False,
        literal_check: Callable[[Any], None] | None = None,
    ) -> ExpressionResult:
        """Resolves an argument as the expression's ``plan_parts`` describe it - one a string names a
        field of as a ``FIELD``, any other as an ``ARGUMENT`` (an ``ENCODED_ARGUMENT`` bound whole).

        Args:
            owner: The expression the argument was passed to.
            attribute: The attribute holding it.
            expression_context: The context it is resolved in.
            treat_str_as_field: Whether a string names a field.
            index: Its position in a sequence held under ``attribute``.
            binds_whole: Whether a list or tuple literal is bound as one parameter.
            literal_check: Refuses a literal the search can't run with - for each later value of a
                kept plan too.

        Returns:
            The result.
        """
        value = getattr(owner, attribute) if index is None else getattr(owner, attribute)[index]
        if treat_str_as_field:
            return ExpressionArguments.get_field_result(owner, attribute, value, expression_context, index)
        # A literal is bound in its field type's stored form, as a Value(...) of it is.
        encoder = (
            None
            if isinstance(value, (Expression, Term))
            else Value.get_literal_encoder(value, expression_context.dialect)
        )
        if literal_check is not None:
            if isinstance(value, Value):
                encoder = SearchArguments.get_checked_encoder(
                    literal_check, Value.get_literal_encoder(value.value, expression_context.dialect)
                )
            elif not isinstance(value, (Expression, Term)):
                encoder = SearchArguments.get_checked_encoder(literal_check, encoder)
        return ExpressionArguments.get_result(
            owner, attribute, value, expression_context, encoder=encoder, index=index, binds_whole=binds_whole
        )

    @staticmethod
    def get_checked_encoder(
        literal_check: Callable[[Any], None], encoder: Callable[[Any], Any] | None
    ) -> Callable[[Any], Any]:
        """``encoder`` refusing first what ``literal_check`` refuses.

        Args:
            literal_check: Refuses a literal.
            encoder: Converts the literal into its bound form; None to bind it as it is.

        Returns:
            The encoder.
        """

        def encode(literal: Any) -> Any:
            literal_check(literal)
            return literal if encoder is None else encoder(literal)

        return encode
