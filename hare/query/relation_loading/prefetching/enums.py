from enum import StrEnum


class PrefetchAttachmentType(StrEnum):
    FORWARD = "forward"
    REVERSE = "reverse"
    MANY = "many"


class PrefetchStrategy(StrEnum):
    DIRECT = "direct"
    REVERSE = "reverse"
    REVERSE_SINGLE = "reverse_single"
    MANY_TO_MANY = "many_to_many"
    LOADED = "loaded"


class PrefetchRowsStrategy(StrEnum):
    ALL = "all"
    EMPTY = "empty"
    NUMBERED = "numbered"
    JOIN = "join"
    LINKS = "links"
