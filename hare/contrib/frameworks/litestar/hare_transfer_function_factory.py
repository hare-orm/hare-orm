from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from litestar.dto._codegen_backend import TransferFunctionFactory

if TYPE_CHECKING:
    from litestar.dto._types import TransferDTOFieldDefinition


class HareTransferFunctionFactory(TransferFunctionFactory):
    """Litestar's generator of a DTO's transfer code, a row read through an attribute accessor kept in a
    local first, and every field of a row read through the accessor.

    Litestar keeps a nested row in a local named after the expression reading it only when the expression is
    an attribute or an item - one of ``HareDTO.attribute_accessor()``'s calls became part of a name of the
    generated code, which then didn't compile (a row two to-one relations deep).
    """

    @contextmanager
    def _access_attribute(self, source_name: str, field_name: str, expect_optional: bool) -> Generator[str]:
        # Litestar reads an optional field only where hasattr() finds it - a related value (``writer_name``)
        # is no attribute of the row; the accessor answers None for a value a row lacks itself.
        with super()._access_attribute(source_name, field_name, expect_optional=False) as value_expression:
            yield value_expression

    def _create_transfer_instance_data(
        self,
        tmp_return_type_name: str,
        source_instance_name: str,
        destination_type_name: str,
        field_definitions: tuple[TransferDTOFieldDefinition, ...],
        destination_type_is_dict: bool,
    ) -> None:
        if "(" in source_instance_name:
            row_name = self._create_local_name("source_row")
            self._add_stmt(f"{row_name} = {source_instance_name}")
            source_instance_name = row_name
        super()._create_transfer_instance_data(
            tmp_return_type_name=tmp_return_type_name,
            source_instance_name=source_instance_name,
            destination_type_name=destination_type_name,
            field_definitions=field_definitions,
            destination_type_is_dict=destination_type_is_dict,
        )
