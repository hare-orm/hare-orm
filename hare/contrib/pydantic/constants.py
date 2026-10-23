from __future__ import annotations

import os

#: Environment variable overriding MODEL_INDEX's entry limit - a user-facing knob against a cache
#: that would otherwise grow one entry per distinct pydantic_model_creator() configuration ever
#: called, forever, for the life of the process.
ENV_PYDANTIC_MODEL_INDEX_MAX_SIZE = "HARE_PYDANTIC_MODEL_INDEX_MAX_SIZE"

#: Default entry limit for MODEL_INDEX - generous enough that realistic schema-generation
#: diversity for a long-running process rarely evicts anything actually still in use.
DEFAULT_PYDANTIC_MODEL_INDEX_MAX_SIZE = 512

#: Environment variable setting how many list wrappers pydantic_queryset_creator() keeps.
ENV_PYDANTIC_QUERYSET_MODEL_INDEX_MAX_SIZE = "HARE_PYDANTIC_QUERYSET_MODEL_INDEX_MAX_SIZE"

#: Default entry limit for QUERYSET_MODEL_INDEX - matches DEFAULT_PYDANTIC_MODEL_INDEX_MAX_SIZE's
#: reasoning; list wrappers are typically requested for the same small set of models as their
#: singular counterparts, so the same generous ceiling applies.
DEFAULT_PYDANTIC_QUERYSET_MODEL_INDEX_MAX_SIZE = 512

MODEL_INDEX_MAX_SIZE = int(os.environ.get(ENV_PYDANTIC_MODEL_INDEX_MAX_SIZE, DEFAULT_PYDANTIC_MODEL_INDEX_MAX_SIZE))

QUERYSET_MODEL_INDEX_MAX_SIZE = int(
    os.environ.get(ENV_PYDANTIC_QUERYSET_MODEL_INDEX_MAX_SIZE, DEFAULT_PYDANTIC_QUERYSET_MODEL_INDEX_MAX_SIZE)
)

# Base32 characters kept of the digest - 80 bits, so two configurations of one model practically
# never collide on a name.
HASH_LENGTH = 16

#: The keys of a field's `constraints` that go into its JSON schema only - pydantic's `Field()` takes
#: no such argument.
JSON_SCHEMA_ONLY_CONSTRAINTS = ("readOnly", "format")

#: The module the generated pydantic models are declared in - the package's public path, so a model's
#: name and hash don't follow the creator's own file.
PYDANTIC_MODELS_MODULE = "hare.contrib.pydantic"

#: The attribute a schema class keeps the checks of its fields in - read on every validated instance.
FETCHED_FIELD_CHECKS_ATTRIBUTE = "_hare_fetched_field_checks"
