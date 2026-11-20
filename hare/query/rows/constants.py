from __future__ import annotations

#: Separates a ``values()`` output key from the index of one column of a composite key it selects
#: - ``values(target=...)`` over a composite foreign key selects ``target__hare_key_component_0``,
#: ``..._1`` and returns them combined as one tuple under ``target``.
COMPOSITE_KEY_COMPONENT_ALIAS_SEPARATOR = "__hare_key_component_"

#: How many namedtuple classes of ``values_list(named=True)`` selections are kept.
ROW_CLASS_CACHE_SIZE = 256
