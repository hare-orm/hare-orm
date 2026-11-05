from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from hare.dialects.enums import ParameterPosition
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.query.expressions.compatibility.result_branch import ResultBranch
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.value import Value
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.sql.terms.values.value_wrapper import ValueWrapper


@dataclass(frozen=True, slots=True)
class BoundExpression:
    """One argument prepared for type selection before its literal is encoded.

    Non-literal children bind once and retain their term, field and joins. Literals
    retain their declaration and origin until the containing operation selects a
    common type and encoder. This object belongs to one build, never to a shared
    expression declaration or a reusable query plan.
    """

    owner: Any
    attribute: str
    value: Any
    branch: ResultBranch
    result: ExpressionResult | None = None
    index: int | None = None

    @classmethod
    def prepare_argument(
        cls,
        owner: Any,
        attribute: str,
        value: Any,
        context: ExpressionContext,
        *,
        label: str | None = None,
        index: int | None = None,
    ) -> BoundExpression:
        """Bind expression inputs while postponing literal conversion and parameter recording."""
        branch = ResultBranch.of(value, None)
        if branch.is_literal:
            return cls(owner, attribute, value, branch, index=index)
        result = ExpressionArguments.get_result(owner, attribute, value, context, index=index)
        output_field = value.get_value_field(result) if isinstance(value, Expression) else result.output_field
        if label is not None:
            EncryptedFieldBase.raise_if_encrypted(output_field, label)
        result = replace(result, output_field=output_field)
        return cls(owner, attribute, value, ResultBranch.of(value, output_field), result, index)

    def lower(
        self,
        context: ExpressionContext,
        *,
        encoder: Callable[[Any], Any] | None = None,
        position: ParameterPosition | None = None,
    ) -> ExpressionResult:
        """Build a literal once with the selected conversion, or reuse an already bound child."""
        if self.result is not None:
            return self.result
        result = ExpressionArguments.get_result(
            self.owner,
            self.attribute,
            self.value,
            context,
            encoder=encoder,
            index=self.index,
        )
        term = result.term
        if position is not None and isinstance(term, ValueWrapper):
            term = Value.get_typed_term(term, term.value, position, context.dialect)
        output_field = (
            self.value.get_value_field(result) if isinstance(self.value, Expression) else result.output_field
        )
        return ExpressionResult(term=term, joins=result.joins, output_field=output_field)
