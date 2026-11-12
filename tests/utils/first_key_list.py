"""A list whose documented iterable view selects only its smallest key."""


class FirstKeyList(list):
    def __iter__(self):
        return iter(sorted(super().__iter__())[:1])
