from enum import IntEnum, StrEnum


class SampleColor(StrEnum):
    RED = "red"
    BLUE = "blue"


class SampleLevel(IntEnum):
    LOW = 1
    HIGH = 2


class ScopedWritePath(StrEnum):
    UPDATE = "update"
    SAVE = "save"
    BULK_UPDATE = "bulk_update"
    DELETE = "delete"
