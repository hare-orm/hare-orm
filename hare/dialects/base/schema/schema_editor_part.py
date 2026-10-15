from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import UnSupportedError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class SchemaEditorPart:
    """A part of a schema editor - the statements of one type of schema change. The editor creates
    each of its parts once, from the part classes it declares; a dialect's editor declares its own
    subclasses of the parts it writes differently."""

    __slots__ = ("editor",)

    def __init__(self, editor: BaseSchemaEditor) -> None:
        """
        Args:
            editor: The schema editor the part belongs to.
        """
        self.editor = editor

    def get_unsupported_error(self, objects_name: str) -> UnSupportedError:
        """The error of a type of schema object the dialect lacks.

        Args:
            objects_name: The objects, in the plural - ``"Sequences"``.

        Returns:
            The error.
        """
        return UnSupportedError(f"{objects_name} are not supported on {self.editor.client.dialect}")
