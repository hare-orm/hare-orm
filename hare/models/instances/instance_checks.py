from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.indexes.index import Index
from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models.model import Model


class InstanceChecks:
    """The checks of a model's Meta made when its schema is created: every entry of Meta.indexes is a
    list of field names or an Index over fields the model has."""

    @staticmethod
    def check(model: type[Model]) -> None:
        """
        Calls various checks to validate the model.

        Args:
            model: The model.

        Raises:
            ConfigurationError: If the model has not been configured correctly.
        """
        InstanceChecks.check_together(model, ModelOption.INDEXES)

    @staticmethod
    def check_together(model: type[Model], together: str) -> None:
        """
        Check the value of a list-of-field-names option.

        Args:
            model: The model.
            together: The name of the ``Meta`` option.

        Raises:
            ConfigurationError: If the model has not been configured correctly.
        """
        _together = getattr(model._meta, together)
        if not isinstance(_together, (tuple, list)):
            raise ConfigurationError(f"'{model.__name__}.{together}' must be a list or tuple.")

        if any(not isinstance(unique_fields, (tuple, list, Index)) for unique_fields in _together):
            raise ConfigurationError(f"All '{model.__name__}.{together}' elements must be lists or tuples.")

        for fields_tuple in _together:
            if isinstance(fields_tuple, Index):
                fields_tuple = fields_tuple.fields
            for field_name in fields_tuple:
                field = model._meta.fields_map.get(field_name)

                if not field:
                    raise ConfigurationError(f"'{model.__name__}.{together}' has no '{field_name}' field.")

                if isinstance(field, ManyToManyFieldInstance):
                    raise ConfigurationError(
                        f"'{model.__name__}.{together}' '{field_name}' field refers to ManyToMany field."
                    )
