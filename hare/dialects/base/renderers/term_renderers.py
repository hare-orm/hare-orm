from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.cache import Cache
from hare.core.registries import Registries

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.context import SqlContext

    #: ``(term, ctx) -> sql``.
    TermRenderer = Callable[[Any, SqlContext], str]
    #: ``(function, ctx) -> name`` - an empty name keeps the function's own.
    FunctionNameRenderer = Callable[[Any, SqlContext], str]


class TermRenderers:
    """How one dialect renders the terms whose SQL differs between dialects, found through a
    term's class hierarchy - a term class of its own uses the renderer of its nearest registered
    base class. A term with no renderer renders its own standard SQL."""

    #: The renderer and the name renderer each set of renderers found for a term class through the
    #: class's bases, kept in a bucket of the set.
    found_renderers: ClassVar[Cache[Any]] = Cache(holds_sql=False, keyed_by_model=False)
    found_name_renderers: ClassVar[Cache[Any]] = Cache(holds_sql=False, keyed_by_model=False)

    def __init__(self) -> None:
        #: The buckets of the caches these renderers read (``Cache.get_owner_bucket()``).
        self.cache_buckets: dict[int, Any] = {}
        self.renderers: dict[type, TermRenderer] = {}
        self.name_renderers: dict[type, FunctionNameRenderer] = {}
        self.renderers_by_term_class: dict[type, TermRenderer | None] = TermRenderers.found_renderers.get_owner_bucket(
            self
        )
        self.name_renderers_by_term_class: dict[type, FunctionNameRenderer | None] = (
            TermRenderers.found_name_renderers.get_owner_bucket(self)
        )
        self.function_renderers: dict[str, TermRenderer] = {}

    def register(self, term_class: type, renderer: TermRenderer) -> None:
        """Sets how the dialect renders ``term_class`` and its subclasses.

        Args:
            term_class: The term class.
            renderer: A ``(term, ctx) -> sql``.
        """
        self.renderers[term_class] = renderer
        TermRenderers.found_renderers.forget_owner(self)
        Registries.changed()

    def register_function(self, function_name: str, renderer: TermRenderer) -> None:
        """Sets how the dialect renders every function called ``function_name`` that has no
        renderer of its own class.

        Args:
            function_name: The SQL function's name, as hare writes it (``"LENGTH"``).
            renderer: A ``(function, ctx) -> sql``.
        """
        self.function_renderers[function_name] = renderer
        Registries.changed()

    def get_function_renderer(self, function_name: str) -> TermRenderer | None:
        """The renderer of the functions called ``function_name``, None for none."""
        return self.function_renderers.get(function_name)

    def register_name(self, function_class: type, name_renderer: FunctionNameRenderer) -> None:
        """Sets the name the dialect calls ``function_class`` and its subclasses by.

        Args:
            function_class: The function class.
            name_renderer: A ``(function, ctx) -> name``; an empty name keeps the function's own.
        """
        self.name_renderers[function_class] = name_renderer
        TermRenderers.found_name_renderers.forget_owner(self)
        Registries.changed()

    def get(self, term_class: type) -> TermRenderer | None:
        """The renderer of ``term_class``, None when the term renders its own SQL."""
        if term_class not in self.renderers_by_term_class:
            self.renderers_by_term_class[term_class] = next(
                (self.renderers[base] for base in term_class.__mro__ if base in self.renderers), None
            )
        return self.renderers_by_term_class[term_class]

    def get_name_renderer(self, function_class: type) -> FunctionNameRenderer | None:
        """The name renderer of ``function_class``, None when the function keeps its own name."""
        if function_class not in self.name_renderers_by_term_class:
            self.name_renderers_by_term_class[function_class] = next(
                (self.name_renderers[base] for base in function_class.__mro__ if base in self.name_renderers), None
            )
        return self.name_renderers_by_term_class[function_class]
