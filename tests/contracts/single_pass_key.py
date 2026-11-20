class SinglePassKey:
    """A domain key whose write representation can be consumed once."""

    def __init__(self, value: int) -> None:
        self.value = value
        self.parts = iter((value,))

    def __iter__(self):
        return self.parts

    def __hash__(self) -> int:
        return hash(self.value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SinglePassKey) and self.value == other.value
