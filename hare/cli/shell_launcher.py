"""Interactive REPL shell launching for the `hare shell` command."""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from hare import Hare
from hare.core.hare_context import HareContext


class ShellLauncher:
    """Builds the shell namespace and launches IPython against it.

    Attributes:
        interactive_shell_class: IPython's embeddable shell, None without IPython installed.
    """

    interactive_shell_class: ClassVar[type[Any] | None] = None

    @staticmethod
    def build_namespace(hare_context: HareContext) -> dict[str, Any]:
        """The names the shell starts with: ``Hare``, the context as ``hare``, ``apps`` and every model - each
        also as ``<app_label>_<ModelName>``.

        Args:
            hare_context: The initialized context.

        Returns:
            The namespace.
        """
        namespace: dict[str, Any] = {
            "Hare": Hare,
            "hare": hare_context,
            "apps": hare_context.apps,
        }
        # Every model goes into the namespace. Two apps' models of one name: the plain name is the
        # last one's, each is also reachable app-qualified, and a warning is printed.
        if hare_context.apps:
            seen_model_names: set[str] = set()
            colliding_model_names: set[str] = set()
            for app_label, models_dict in hare_context.apps.items():
                for model_name, model_class in models_dict.items():
                    if model_name in seen_model_names:
                        colliding_model_names.add(model_name)
                    else:
                        seen_model_names.add(model_name)
                    namespace[model_name] = model_class
                    namespace[f"{app_label}_{model_name}"] = model_class
            if colliding_model_names:
                for model_name in sorted(colliding_model_names):
                    print(  # interactive shell banner, matches this file's own launch_*() prints
                        f"Warning: multiple apps register a model named {model_name!r} - "
                        f"{model_name!r} refers to whichever app loaded last; every one is also "
                        f"reachable as <app_label>_{model_name}."
                    )
        return namespace

    @staticmethod
    def _shell_models_info(namespace: dict[str, Any]) -> str:
        """The "Available models: ..."/"No models loaded" banner line."""
        model_names = [name for name in namespace if name not in {"Hare", "hare", "apps"}]
        return f"Available models: {', '.join(model_names)}" if model_names else "No models loaded"

    @classmethod
    def launch_ipython(cls, namespace: dict[str, Any], loop_runner: Callable[[Awaitable[Any]], Any]) -> None:
        """Runs IPython with top-level ``await`` on the event loop the Hare context was opened on.

        Args:
            namespace: The shell's namespace.
            loop_runner: The session's callable for evaluating an asynchronous cell.
        """
        assert cls.interactive_shell_class is not None  # nosec B101
        banner = (
            "Hare ORM Shell (IPython with async support)\n"
            f"{cls._shell_models_info(namespace)}\n"
            "Use 'await' directly for async operations (e.g., 'await YourModel.objects.all()').\n"
        )
        with contextlib.suppress(EOFError):
            shell = cls.interactive_shell_class(user_ns=namespace, banner1=banner, loop_runner=loop_runner)
            shell.autoawait = True
            shell()


try:
    from IPython.terminal.embed import InteractiveShellEmbed

    ShellLauncher.interactive_shell_class = InteractiveShellEmbed
except ImportError:
    pass
