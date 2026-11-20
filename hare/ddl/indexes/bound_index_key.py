from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


@dataclass(frozen=True, slots=True)
class BoundIndexKey:
    """A physical column name or a rendered expression, identified by its declaration.

    SQL punctuation never determines whether a key is a column. Expressions already
    include required parentheses and their declared ordering; columns are quoted only
    when the final index statement is rendered.
    """

    value: str
    is_expression: bool = False

    def get_sql(self, editor: BaseSchemaEditor) -> str:
        """Render a known column or preserve the prepared expression verbatim."""
        return self.value if self.is_expression else editor.quote(self.value)
