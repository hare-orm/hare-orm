from __future__ import annotations

import importlib
from types import ModuleType
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast, overload

from hare.exceptions import ConfigurationError
from hare.fields.constants import E164_PHONE_PATTERN
from hare.fields.data.constants import PHONE_FIELD_MAX_LENGTH
from hare.fields.data.text.char_field import CharField, TStr
from hare.fields.validators.exceptions import InvalidPhoneNumber
from hare.fields.validators.formats.e164_phone_validator import E164PhoneValidator

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class PhoneField(CharField[TStr]):
    """A phone number, written in E.164 form: ``+16502530000``.

    With the ``phonenumbers`` package installed (``pip install hare-orm[phone]``) a number in any
    form it reads is accepted - ``+1 (650) 253-0000``, and with ``region`` a national one,
    ``(650) 253-0000`` - when it finds the number valid, and written in E.164 form. Without the
    package only a number already in E.164 form is accepted. Filter values are converted the same
    way, so a number is found whatever form it is given in.

    Args:
        region: The region a number without a country code is in - an ISO 3166-1 code
            (``"US"``) ``phonenumbers`` knows. Needs ``phonenumbers``.
    """

    #: The ``phonenumbers`` module - None when it isn't installed. Imported by the first
    #: ``PhoneField`` made: it takes longer to import than all of ``hare.fields``.
    phonenumbers: ClassVar[ModuleType | None] = None
    #: Whether ``phonenumbers`` was looked for.
    phonenumbers_looked_up: ClassVar[bool] = False

    @overload
    def __init__(
        self: PhoneField[str], *, region: str | None = None, null: Literal[False] = False, **kwargs: Any
    ) -> None: ...

    @overload
    def __init__(
        self: PhoneField[str | None], *, region: str | None = None, null: Literal[True], **kwargs: Any
    ) -> None: ...

    def __init__(self, *, region: str | None = None, **kwargs: Any) -> None:
        phonenumbers = type(self).import_phonenumbers()
        if region is not None:
            if phonenumbers is None:
                raise ConfigurationError(
                    "PhoneField: region= needs the 'phonenumbers' package - install it with "
                    "`pip install hare-orm[phone]`."
                )
            if not isinstance(region, str) or region not in phonenumbers.SUPPORTED_REGIONS:
                raise ConfigurationError(
                    f"PhoneField: region must be an ISO 3166-1 region code phonenumbers knows, such as 'US', "
                    f"got {region!r}"
                )
        self.region = region
        # CharField's overloads bind self to CharField[str] or CharField[str | None] - TStr is one of them.
        super().__init__(PHONE_FIELD_MAX_LENGTH, **kwargs)  # type: ignore[misc]
        #: The validator checking the number - made by the write codec itself.
        self.format_validator = E164PhoneValidator()
        self.validators.append(self.format_validator)

    @classmethod
    def import_phonenumbers(cls) -> ModuleType | None:
        """Imports ``phonenumbers`` once.

        Returns:
            The module; None when it isn't installed.
        """
        if not PhoneField.phonenumbers_looked_up:
            try:
                PhoneField.phonenumbers = importlib.import_module("phonenumbers")
            except ImportError:
                PhoneField.phonenumbers = None
            PhoneField.phonenumbers_looked_up = True
        return cls.phonenumbers

    @property
    def constraints(self) -> dict[str, Any]:
        if self.phonenumbers is not None:
            # A number is accepted in any form the package reads.
            return super().constraints
        return {**super().constraints, "pattern": f"^{E164_PHONE_PATTERN}$"}

    def get_e164_number(self, number: str) -> str:
        """The number in E.164 form, as ``phonenumbers`` reads it.

        Args:
            number: The number in any form.

        Returns:
            The number in E.164 form.

        Raises:
            ValidationError: ``phonenumbers`` can't read the number or finds it invalid.
        """
        phonenumbers = cast("ModuleType", self.phonenumbers)
        try:
            parsed_number = phonenumbers.parse(number, self.region)
        except phonenumbers.NumberParseException:
            parsed_number = None
        if parsed_number is None or not phonenumbers.is_valid_number(parsed_number):
            raise self.get_validation_error(InvalidPhoneNumber(), number)
        return str(phonenumbers.format_number(parsed_number, phonenumbers.PhoneNumberFormat.E164))

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        if isinstance(value, str) and self.phonenumbers is not None:
            value = self.get_e164_number(value)
        return super().to_db_value(value, instance)
