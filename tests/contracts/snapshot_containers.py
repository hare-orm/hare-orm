"""Ordinary container subclasses accepted by membership and JSON value conversion."""


class KeyList(list):
    pass


class KeySet(set):
    pass


class JsonDocument(dict):
    pass
