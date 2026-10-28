from __future__ import annotations

from collections.abc import Iterable, Sequence

from hare.contrib.outbox.relay.constants import TOPIC_PATTERN_WILDCARDS, TOPIC_PREFIX_WILDCARD
from hare.exceptions import ConfigurationError
from hare.query.enums import Connector
from hare.query.expressions import Q


class TopicSelection:
    """The topics a relay takes - names and prefix patterns (``shop.*``), some of them excluded - as a
    condition on the outbox's ``topic`` and as a check of a wakeup's topics.

    Args:
        topics: The names and prefix patterns taken - every topic when None.
        exclude_topics: The names and prefix patterns left out.

    Raises:
        ConfigurationError: A pattern with ``*`` or ``?`` anywhere but a ``*`` at its end.
    """

    def __init__(self, topics: Sequence[str] | None = None, exclude_topics: Sequence[str] = ()) -> None:
        self.topics = tuple(topics) if topics is not None else None
        self.exclude_topics = tuple(exclude_topics)
        self.names, self.prefixes = TopicSelection.get_names_and_prefixes(self.topics or ())
        self.excluded_names, self.excluded_prefixes = TopicSelection.get_names_and_prefixes(self.exclude_topics)

    @staticmethod
    def get_names_and_prefixes(patterns: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The topic names and the prefixes of the prefix patterns among some patterns.

        Args:
            patterns: Names and prefix patterns.

        Returns:
            The names and the prefixes.

        Raises:
            ConfigurationError: A pattern with ``*`` or ``?`` anywhere but a ``*`` at its end.
        """
        names: list[str] = []
        prefixes: list[str] = []
        for pattern in patterns:
            is_prefix = pattern.endswith(TOPIC_PREFIX_WILDCARD)
            text = pattern[: -len(TOPIC_PREFIX_WILDCARD)] if is_prefix else pattern
            if TOPIC_PATTERN_WILDCARDS.intersection(text):
                raise ConfigurationError(
                    f"The topic pattern {pattern!r} has a wildcard inside it - a pattern is a name or a prefix "
                    f"ending in {TOPIC_PREFIX_WILDCARD!r}"
                )
            (prefixes if is_prefix else names).append(text)
        return tuple(names), tuple(prefixes)

    @staticmethod
    def get_matching_condition(names: Sequence[str], prefixes: Sequence[str]) -> Q | None:
        """The condition of a topic being one of some names or starting with one of some prefixes.

        Args:
            names: The names.
            prefixes: The prefixes.

        Returns:
            The condition, None for no names and no prefixes.
        """
        conditions = [Q(topic__startswith=prefix) for prefix in prefixes]
        if names:
            conditions.insert(0, Q(topic__in=list(names)))
        if not conditions:
            return None
        return Q.with_connector(Connector.OR, *conditions)

    @staticmethod
    def is_matching(topic: str, names: Sequence[str], prefixes: Sequence[str]) -> bool:
        """Whether a topic is one of some names or starts with one of some prefixes.

        Args:
            topic: The topic.
            names: The names.
            prefixes: The prefixes.

        Returns:
            True when it is.
        """
        return topic in names or topic.startswith(tuple(prefixes))

    def get_condition(self) -> Q | None:
        """The condition on the outbox's ``topic`` of the events taken.

        Returns:
            The condition, None when every event is taken.
        """
        excluded = TopicSelection.get_matching_condition(self.excluded_names, self.excluded_prefixes)
        if self.topics is None:
            return None if excluded is None else ~excluded
        # No topics at all take no event - an empty IN.
        included = TopicSelection.get_matching_condition(self.names, self.prefixes) or Q(topic__in=[])
        return included if excluded is None else included & ~excluded

    def takes(self, topic: str) -> bool:
        """Whether a topic is one of those taken.

        Args:
            topic: The topic.

        Returns:
            True when it is.
        """
        if TopicSelection.is_matching(topic, self.excluded_names, self.excluded_prefixes):
            return False
        return self.topics is None or TopicSelection.is_matching(topic, self.names, self.prefixes)
