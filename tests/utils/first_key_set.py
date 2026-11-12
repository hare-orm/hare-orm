"""A set whose iterable view selects only its smallest key."""


class FirstKeySet(set):
    def __iter__(self):
        return iter(sorted(super().__iter__())[:1])
