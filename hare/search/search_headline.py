from __future__ import annotations

from typing import ClassVar

from hare.exceptions import ConfigurationError
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.search.constants import (
    SEARCH_HEADLINE_MAX_COUNT,
    SEARCH_HEADLINE_MAX_MARKER_LENGTH,
    TEXT_SEARCH_CONFIGURATIONS_REQUIRED_FEATURE,
)
from hare.search.query.search_query import SearchQuery
from hare.search.search_features import SearchFeatures
from hare.search.types import ConfigInput, HeadlineExpressionInput, HeadlineOptionValue, QueryInput
from hare.sql.terms.term import Term


class SearchHeadline(Expression):
    """A text with the words a full-text search matches marked, for an annotation. PostgreSQL
    marks any text with ``ts_headline()``; SQLite marks a field of the model's ``FullTextIndex``
    with FTS5's ``highlight()``, or with ``max_words`` its ``snippet()`` - the fragment of at most
    that many words (64 at most) matching best - and gives a row the query doesn't match its text
    as it is.

    Example: ``Article.objects.annotate(excerpt=SearchHeadline("body", "hare orm", max_words=20))``

    Args:
        expression: The field whose text is marked, or (on PostgreSQL) an expression giving it.
        query: The search text (``SearchType.PLAIN``) or a ``SearchQuery``.
        config: The text search configuration (``"english"``).
        start_sel: The text before a matched word - ``<b>`` by default.
        stop_sel: The text after it - ``</b>`` by default.
        max_words: The most words of a fragment.
        min_words: The fewest words of a fragment.
        short_word: Words this short or shorter are dropped at a fragment's ends.
        highlight_all: Mark the whole text instead of fragments of it.
        max_fragments: The most fragments.
        fragment_delimiter: The text between fragments - `` ... `` by default.

        ``config``, ``min_words``, ``short_word``, ``highlight_all`` and ``max_fragments`` need
        ``features.supports_text_search_configurations``.

    Raises:
        ConfigurationError: An argument of the wrong type or out of range.
    """

    #: The arguments that need ``features.supports_text_search_configurations``.
    configuration_option_names = ("min_words", "short_word", "highlight_all", "max_fragments")

    #: The source text, the query and the configuration, bound; the options are written into the SQL
    #: text.
    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("get_plan_options", PlanPartType.KEY_METHOD),
        ("start_sel", PlanPartType.NONE),
        ("stop_sel", PlanPartType.NONE),
        ("fragment_delimiter", PlanPartType.NONE),
        ("max_words", PlanPartType.NONE),
        ("min_words", PlanPartType.NONE),
        ("short_word", PlanPartType.NONE),
        ("max_fragments", PlanPartType.NONE),
        ("highlight_all", PlanPartType.NONE),
        ("expression", PlanPartType.FIELD),
        ("query", PlanPartType.NONE),
        ("query_argument", PlanPartType.ARGUMENT),
        ("config", PlanPartType.ARGUMENT),
    )

    def __init__(
        self,
        expression: HeadlineExpressionInput,
        query: QueryInput,
        config: ConfigInput | None = None,
        start_sel: str | None = None,
        stop_sel: str | None = None,
        max_words: int | None = None,
        min_words: int | None = None,
        short_word: int | None = None,
        highlight_all: bool | None = None,
        max_fragments: int | None = None,
        fragment_delimiter: str | None = None,
    ) -> None:
        self.expression = expression
        self.query = query
        #: The query as an expression - search text as a ``SearchQuery``.
        self.query_argument = query if isinstance(query, (Expression, Term)) else SearchQuery(query)
        self.config = config
        self.start_sel = self.get_checked_text("start_sel", start_sel)
        self.stop_sel = self.get_checked_text("stop_sel", stop_sel)
        self.fragment_delimiter = self.get_checked_text("fragment_delimiter", fragment_delimiter)
        self.max_words = self.get_checked_count("max_words", max_words, 1)
        self.min_words = self.get_checked_count("min_words", min_words, 0)
        self.short_word = self.get_checked_count("short_word", short_word, 0)
        self.max_fragments = self.get_checked_count("max_fragments", max_fragments, 0)
        if highlight_all is not None and not isinstance(highlight_all, bool):
            raise ConfigurationError(f"SearchHeadline highlight_all must be a bool, got {highlight_all!r}")
        self.highlight_all = highlight_all

    @staticmethod
    def get_checked_text(argument_name: str, value: str | None) -> str | None:
        """A marker or delimiter argument.

        Args:
            argument_name: The argument's name, for the message.
            value: The given value.

        Returns:
            The text, None when not given.

        Raises:
            ConfigurationError: The value isn't text of at most 1000 characters without a NUL.
        """
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > SEARCH_HEADLINE_MAX_MARKER_LENGTH or "\x00" in value:
            raise ConfigurationError(
                f"SearchHeadline {argument_name} must be text of at most {SEARCH_HEADLINE_MAX_MARKER_LENGTH} "
                f"characters, got {value!r}"
            )
        return value

    @staticmethod
    def get_checked_count(argument_name: str, value: int | None, minimum: int) -> int | None:
        """A word or fragment count argument.

        Args:
            argument_name: The argument's name, for the message.
            value: The given value.
            minimum: The smallest count.

        Returns:
            The count, None when not given.

        Raises:
            ConfigurationError: The value isn't an int from ``minimum`` to 100000.
        """
        if value is None:
            return None
        if type(value) is not int or not minimum <= value <= SEARCH_HEADLINE_MAX_COUNT:
            raise ConfigurationError(
                f"SearchHeadline {argument_name} must be an int from {minimum} to {SEARCH_HEADLINE_MAX_COUNT}, "
                f"got {value!r}"
            )
        return value

    def get_options(self) -> dict[str, HeadlineOptionValue]:
        """The given marker, delimiter, count and flag arguments.

        Returns:
            Their values by argument name, in the order of the arguments.
        """
        options = {
            "start_sel": self.start_sel,
            "stop_sel": self.stop_sel,
            "max_words": self.max_words,
            "min_words": self.min_words,
            "short_word": self.short_word,
            "highlight_all": self.highlight_all,
            "max_fragments": self.max_fragments,
            "fragment_delimiter": self.fragment_delimiter,
        }
        return {name: value for name, value in options.items() if value is not None}

    def get_plan_options(self) -> tuple[tuple[str, HeadlineOptionValue], ...]:
        """The options given, as the plan key holds them.

        Returns:
            Each option's name and value.
        """
        return tuple(self.get_options().items())

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        if self.config is not None:
            SearchFeatures.raise_if_unsupported(
                expression_context, "SearchHeadline(config=...)", TEXT_SEARCH_CONFIGURATIONS_REQUIRED_FEATURE
            )
        for option_name in self.configuration_option_names:
            if getattr(self, option_name) is not None:
                SearchFeatures.raise_if_unsupported(
                    expression_context,
                    f"SearchHeadline({option_name}=...)",
                    TEXT_SEARCH_CONFIGURATIONS_REQUIRED_FEATURE,
                )
        return expression_context.dialect.text_search.get_headline_result(self, expression_context)
