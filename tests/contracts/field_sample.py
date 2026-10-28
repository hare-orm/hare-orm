from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

import pytest

from hare.fields.field import Field
from tests.contracts.constants import SAMPLED_FIELD_NAME


@dataclasses.dataclass(frozen=True)
class FieldSample:
    """A declaration of one field class as a model declares it, and values of it - what the contract tests
    check a field class with, in every place a field's value goes.

    Attributes:
        name: The sample's name - the test's id.
        make_fields: Makes the fields of the sample's model, besides its key: the sampled one under
            ``SAMPLED_FIELD_NAME``, and those it needs (the source of a generated field).
        values: Values written to the field.
        json_values: The JSON form of each value - what an outbox event holds of it.
        requires: The extras the field needs, as ``@pytest.mark.requires`` names them.
        related: Whether the field is a relation to the contract tests' target model.
        column_name: The name the values are written under and an event holds them under - a foreign
            key's column.
        captured: Whether a captured change holds the field - a sensitive field it leaves out.
        value_dialects: The dialects storing the values, None for each the field has a column on - SQLite
            refuses a NaN.
        registered_live: Whether the sample's models can be registered while the application runs - a generic
            foreign key targets configured models only.
        request_values: What a request gives for each value, the text of an address; None for the values
            themselves.
        request_filter_refused: Whether a request query refuses to filter by the field - one with no type of
            its value to validate a parameter with.
        request_filtered: Whether a request can give the values - one filtering by a multirange gives a single
            range, never the several a row holds.
    """

    name: str
    make_fields: Callable[[], dict[str, Field[Any]]]
    values: tuple[Any, ...] = ()
    json_values: tuple[Any, ...] = ()
    requires: tuple[str, ...] = ()
    related: bool = False
    column_name: str = SAMPLED_FIELD_NAME
    captured: bool = True
    value_dialects: tuple[str, ...] | None = None
    registered_live: bool = True
    request_values: tuple[Any, ...] | None = None
    request_filter_refused: bool = False
    request_filtered: bool = True

    def get_parameter(self) -> Any:
        """The sample as a test's parameter, requiring its extras.

        Returns:
            The parameter, named after the sample.
        """
        marks = [pytest.mark.requires(*self.requires)] if self.requires else []
        return pytest.param(self, id=self.name, marks=marks)

    def get_field_class(self) -> type[Field[Any]]:
        """The class the sample is of.

        Returns:
            The class of its sampled field.
        """
        return type(self.make_fields()[SAMPLED_FIELD_NAME])
