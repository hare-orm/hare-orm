from enum import StrEnum


class MetaVariant(StrEnum):
    """How a concrete model declares its ``Meta`` under an abstract base declaring an option."""

    #: No ``Meta`` at all.
    NO_META = "no_meta"
    #: A ``class Meta`` naming nothing.
    BARE_META = "bare_meta"
    #: A ``class Meta(Base.Meta)`` naming nothing.
    META_OF_BASE = "meta_of_base"
    #: A ``Meta`` naming another value of the option.
    OVERRIDDEN = "overridden"
    #: A ``Meta`` naming ``None`` for the option.
    NONE = "none"
