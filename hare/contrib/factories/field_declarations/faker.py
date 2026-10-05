from __future__ import annotations

import importlib
from typing import Any, ClassVar

from hare.contrib.factories.constants import FAKER_CACHE_MAX_SIZE, FAKER_MISSING_MESSAGE, FAKER_MODULE
from hare.contrib.factories.field_declarations.declaration import Declaration
from hare.core.caching.cache import Cache
from hare.exceptions import ConfigurationError


class Faker(Declaration):
    """A value of the ``faker`` library's provider - ``pip install faker``::

        name = Faker("name")
        city = Faker("city", locale="de_DE")

    Args:
        provider: The provider's name.
        locale: The provider's locale, faker's default without it.
        arguments: The provider's arguments.
    """

    #: A ``faker.Faker`` by locale - made on the first value of each.
    fakers_by_locale: ClassVar[Cache[Any]] = Cache(FAKER_CACHE_MAX_SIZE, holds_sql=False, keyed_by_model=False)

    def __init__(self, provider: str, *, locale: str | None = None, **arguments: Any) -> None:
        self.provider = provider
        self.locale = locale
        self.arguments = arguments

    @classmethod
    def get_faker(cls, locale: str | None) -> Any:
        """The ``faker.Faker`` of a locale.

        Raises:
            ConfigurationError: ``faker`` isn't installed.
        """
        faker = cls.fakers_by_locale.get((locale,))
        if faker is None:
            try:
                faker_module = importlib.import_module(FAKER_MODULE)
            except ModuleNotFoundError:
                raise ConfigurationError(FAKER_MISSING_MESSAGE) from None
            faker = cls.fakers_by_locale[(locale,)] = faker_module.Faker(locale)
        return faker

    def evaluate(self, sequence_number: int) -> Any:
        return getattr(Faker.get_faker(self.locale), self.provider)(**self.arguments)
