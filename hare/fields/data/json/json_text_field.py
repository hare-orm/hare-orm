from __future__ import annotations

from typing import Any

from hare.fields.data.json.json_field import JSONField


class JSONTextField(JSONField[Any]):
    """A JSON field kept as its JSON text where a database's JSON type keeps less than the text does (a
    null of a key, a list at the top) - every JSON value read back as written: the outbox's payload and
    headers."""

    keeps_json_text = True
