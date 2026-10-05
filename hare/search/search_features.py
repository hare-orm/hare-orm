from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import UnSupportedError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions import ExpressionContext


class SearchFeatures:
    """Checks a connection runs a part of ``hare.search`` before a query is sent."""

    @staticmethod
    def raise_if_unsupported(expression_context: ExpressionContext, name: str, feature: str) -> None:
        """Rejects a search expression, or one of its arguments, on a connection without the
        feature - the dialect's own features while the connection isn't chosen yet.

        Args:
            expression_context: The context the expression is resolved in.
            name: The expression or argument, named in the error.
            feature: The ``Features`` flag it needs.

        Raises:
            UnSupportedError: The connection hasn't the flag.
        """
        connection = expression_context.connection
        features = connection.features if connection is not None else expression_context.dialect.features
        if not getattr(features, feature):
            where = (
                f"the {connection.connection_alias!r} connection"
                if connection is not None
                else f"the {expression_context.dialect} dialect"
            )
            raise UnSupportedError(f"{name} can't run on {where}: it needs features.{feature}, which it doesn't have")
