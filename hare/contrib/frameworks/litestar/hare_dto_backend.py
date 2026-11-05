from __future__ import annotations

from typing import TYPE_CHECKING, Any

from litestar.dto._codegen_backend import DTOCodegenBackend

from hare.contrib.frameworks.litestar.hare_transfer_function_factory import HareTransferFunctionFactory

if TYPE_CHECKING:
    from litestar.typing import FieldDefinition


class HareDTOBackend(DTOCodegenBackend):
    """Select Hare's attribute reader locally, retaining Litestar's DTO code generator.

    The interpreted backend cannot transfer nullable collections of nested models.
    A local factory supports that existing DTO contract without changing any
    Litestar module attribute or the backend used by another integration.
    """

    def _create_transfer_data_fn(self, destination_type: type[Any], field_definition: FieldDefinition) -> Any:
        return HareTransferFunctionFactory.create_transfer_data(
            destination_type=destination_type,
            field_definitions=self.parsed_field_definitions,
            is_data_field=self.is_data_field,
            field_definition=field_definition,
            attribute_accessor=self.attribute_accessor,
        )
