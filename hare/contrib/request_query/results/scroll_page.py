import dataclasses


# Annotations evaluated at class creation: before Python 3.13 typing.get_type_hints() (which
# framework integrations read the fields with) can't resolve a PEP 695 type parameter from text.
@dataclasses.dataclass(slots=True)
class ScrollPage[ItemType]:
    """One page of a ``ScrollPagination`` - an offset page without the total count.

    Attributes:
        result: The rows of the page.
        limit: The page size.
        offset: How many matching rows precede the page.
        next: The address of the next page, None on the last one or without a request address.
        previous: The address of the previous page, None on the first one or without a request
            address.
    """

    result: list[ItemType]
    limit: int
    offset: int
    next: str | None
    previous: str | None
