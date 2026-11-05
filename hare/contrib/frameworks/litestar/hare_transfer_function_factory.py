from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from litestar.dto._codegen_backend import TransferFunctionFactory
from litestar.dto._types import CollectionType, MappingType

if TYPE_CHECKING:
    from litestar.dto._types import TransferDTOFieldDefinition, TransferType
    from litestar.typing import FieldDefinition


class HareTransferFunctionFactory(TransferFunctionFactory):
    """Keep accessor calls in local values and recursive factories within this DTO.

    Generated identifiers must never contain an attribute-access expression. Reading
    via the accessor also includes computed and flattened fields absent from the row's
    attributes. Litestar's recursive collection branches name their factory directly;
    these two hooks propagate this local factory instead, leaving the library intact.
    """

    @contextmanager
    def _access_attribute(self, source_name: str, field_name: str, expect_optional: bool) -> Generator[str]:
        with super()._access_attribute(source_name, field_name, expect_optional=False) as expression:
            value_name = self._create_local_name("attribute_value")
            self._add_stmt(f"{value_name} = {expression}")
            yield value_name

    def _create_transfer_data_body_nested(
        self,
        field_definition: FieldDefinition,
        field_definitions: tuple[TransferDTOFieldDefinition, ...],
        destination_type: type[Any],
        source_data_name: str,
        assignment_target: str,
    ) -> None:
        transfer = type(self).create_transfer_data(
            is_data_field=self.is_data_field,
            destination_type=destination_type,
            field_definition=field_definition.inner_types[0],
            field_definitions=field_definitions,
            attribute_accessor=self.attribute_accessor,
        )
        self.write_collection(
            transfer,
            field_definition,
            source_data_name,
            assignment_target,
            is_mapping=field_definition.is_mapping,
        )

    def _create_transfer_type_data_body(
        self,
        transfer_type: TransferType,
        nested_as_dict: bool,
        source_value_name: str,
        assignment_target: str,
    ) -> None:
        if isinstance(transfer_type, (CollectionType, MappingType)) and transfer_type.has_nested:
            transfer = type(self).create_transfer_type_data(
                is_data_field=self.is_data_field,
                transfer_type=transfer_type.value_type
                if isinstance(transfer_type, MappingType)
                else transfer_type.inner_type,
                attribute_accessor=self.attribute_accessor,
            )
            self.write_collection(
                transfer,
                transfer_type.field_definition,
                source_value_name,
                assignment_target,
                is_mapping=isinstance(transfer_type, MappingType),
            )
            return
        super()._create_transfer_type_data_body(transfer_type, nested_as_dict, source_value_name, assignment_target)

    def write_collection(
        self,
        transfer: Any,
        definition: FieldDefinition,
        source: str,
        target: str,
        *,
        is_mapping: bool,
    ) -> None:
        """Emit Litestar's collection construction with the local nested transfer function."""
        origin = self._add_to_fn_globals("origin", definition.instantiable_origin)
        function = self._add_to_fn_globals("transfer_data", transfer)
        values = (
            f"(key, {function}(item)) for key, item in {source}.items()"
            if is_mapping
            else f"{function}(item) for item in {source}"
        )
        self._add_stmt(f"{target} = {origin}({values})")
