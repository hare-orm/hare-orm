from typing import Any

from hare.exceptions import ValidationError
from hare.fields.validators.validator import Validator


class LengthValidator(Validator):
    # str for CharField/CharEnumField's own length constraint; list/tuple/bytes for anything else
    # that legitimately attaches a Max/MinLengthValidator to a container-typed field (e.g.
    # an element-count check on an ArrayField).
    types = (str, list, tuple, bytes)

    def _validate_type(self, value: Any) -> None:
        if not isinstance(value, self.types):
            raise ValidationError("Value must be a string, list, tuple, or bytes, and is required")
