from enum import StrEnum


class WriteRole(StrEnum):
    """Which of the write matrix's models a change is of."""

    SUBJECT = "subject"
    NOTE = "note"
    TAG = "tag"
    LINK = "link"


class WriteOutcome(StrEnum):
    """What a write path does to a thing a write observer watches."""

    #: The observer sees the write and holds what the contract says of it.
    OBSERVED = "observed"
    #: The write is none the observer is about - it takes no value and touches no row it watches.
    NOT_APPLICABLE = "not_applicable"
    #: The write is refused for the watched reason.
    REFUSED = "refused"
