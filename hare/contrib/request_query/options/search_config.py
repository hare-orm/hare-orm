from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from hare.contrib.request_query.enums import ParameterType
from hare.contrib.request_query.options.constants import DEFAULT_SEARCH_PARAMETER
from hare.contrib.request_query.options.parameter_field import ParameterField
from hare.contrib.request_query.options.request_option import RequestOption
from hare.exceptions import ConfigurationError
from hare.query.enums import Connector, Lookup
from hare.query.expressions import Q


@dataclasses.dataclass(frozen=True, slots=True)
class SearchConfig(RequestOption):
    """A text search over several fields: one parameter, one lookup per field, the fields joined
    by ``join_type``.

    Args:
        fields: The fields searched - fields or paths through relations.
        lookup: The lookup each field is searched with - ``icontains``, or a lookup of a dialect
            (``search``, ``trigram_similar`` on PostgreSQL, in a PostgreSQL request query).
        parameter: The name of the parameter.
        join_type: How the fields' conditions join - ``Connector.OR`` (any field matches) or ``Connector.AND``.
        split_words: Whether the text is searched word by word: every word must match, each in
            the fields joined by ``join_type`` - ``?search=ursula wizard`` finds a book by Ursula
            titled "Wizard". False searches the whole text as one value.
    """

    fields: tuple[str, ...]
    lookup: Lookup | str = Lookup.ICONTAINS
    parameter: str = DEFAULT_SEARCH_PARAMETER
    join_type: Connector = Connector.OR
    split_words: bool = False

    def __post_init__(self) -> None:
        self.check_allowed_names(type(self).__name__, self.fields)
        if not isinstance(self.lookup, str):
            raise ConfigurationError(f"SearchConfig.lookup must be a lookup name, got {self.lookup!r}")
        self.check_parameter_name(type(self).__name__, self.parameter)
        if self.join_type not in {Connector.AND, Connector.OR}:
            raise ConfigurationError(
                f"SearchConfig.join_type must be Connector.AND or Connector.OR, got {self.join_type!r}"
            )
        if not isinstance(self.split_words, bool):
            raise ConfigurationError(f"SearchConfig.split_words must be a bool, got {self.split_words!r}")

    def get_parameter_fields(self) -> tuple[ParameterField, ...]:
        return (ParameterField(self.parameter, str | None, None, ParameterType.SEARCH),)

    def get_filter_key(self, field_name: str) -> str:
        """The ``.filter()`` key searching one field.

        Args:
            field_name: One of ``fields``.

        Returns:
            The field with the lookup, or the bare field for plain equality.
        """
        return f"{field_name}__{self.lookup}" if self.lookup else field_name

    def get_condition(self, values: Mapping[str, Any]) -> Q | None:
        """The condition of a request's search text.

        Args:
            values: The request query's values by parameter.

        Returns:
            The fields' conditions joined by ``join_type`` - one such condition per word, joined
            with ``AND``, when ``split_words`` - None without a text.
        """
        text = values.get(self.parameter)
        if text is None or not text.strip():
            return None
        words = text.split() if self.split_words else [text]
        return Q(
            *(
                Q.with_connector(
                    self.join_type, *(Q(**{self.get_filter_key(field_name): word}) for field_name in self.fields)
                )
                for word in words
            )
        )
