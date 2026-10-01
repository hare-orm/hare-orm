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
