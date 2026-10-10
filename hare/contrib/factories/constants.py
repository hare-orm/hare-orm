from __future__ import annotations

#: The library ``Faker`` declarations take their values from.
FAKER_MODULE = "faker"
FAKER_MISSING_MESSAGE = "Faker() declarations need the faker library - pip install faker"
#: The most locales whose ``faker.Faker`` is kept.
FAKER_CACHE_MAX_SIZE = 32
#: The inner class of a factory holding its parameters and traits.
PARAMETERS_CLASS_NAME = "Params"
#: What ``ModelFactory`` keeps on each factory class - never a field's declaration.
FACTORY_OWN_ATTRIBUTES = frozenset(
    {"factory_model", "factory_declarations", "factory_parameters", "factory_sequence_number"}
)
