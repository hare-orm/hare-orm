"""Interactive REPL shell launching for the `hare shell` command."""

import contextlib
from types import ModuleType
from typing import Any, ClassVar

from hare import Hare
from hare.core.context import HareContext


class ShellLauncher:
    """Builds the shell namespace and launches IPython against it.

    Attributes:
        interactive_shell_class: IPython's embeddable shell, None without IPython installed.
        nest_asyncio: The ``nest_asyncio`` module IPython's cells run on the open event loop with.
    """

    interactive_shell_class: ClassVar[type[Any] | None] = None
    nest_asyncio: ClassVar[ModuleType | None] = None

    @staticmethod
    def build_namespace(hare_ctx: HareContext) -> dict[str, Any]:
        # Prepare namespace with Hare context and useful imports
        namespace: dict[str, Any] = {
            "Hare": Hare,
            "hare": hare_ctx,
            "apps": hare_ctx.apps,
        }
        # Every model goes into the namespace. Two apps' models of one name: the plain name is the
        # last one's, each is also reachable app-qualified, and a warning is printed.
        if hare_ctx.apps:
            owning_app_label_by_model_name: dict[str, str] = {}
            colliding_model_names: set[str] = set()
            for app_label, models_dict in hare_ctx.apps.items():
                for model_name, model_class in models_dict.items():
                    if model_name in owning_app_label_by_model_name:
                        colliding_model_names.add(model_name)
                    else:
                        owning_app_label_by_model_name[model_name] = app_label
                    namespace[model_name] = model_class
                    namespace[f"{app_label}_{model_name}"] = model_class
            if colliding_model_names:
                for model_name in sorted(colliding_model_names):
                    print(  # noqa: T201 - interactive shell banner, matches this file's own launch_*() prints
                        f"Warning: multiple apps register a model named {model_name!r} - "
                        f"{model_name!r} refers to whichever app loaded last; every one is also "
                        f"reachable as <app_label>_{model_name}."
                    )
        return namespace

    @staticmethod
    def _shell_models_info(namespace: dict[str, Any]) -> str:
        """The "Available models: ..."/"No models loaded" banner line."""
        model_names = [k for k in namespace.keys() if k not in ("Hare", "hare", "connections", "apps")]
        return f"Available models: {', '.join(model_names)}" if model_names else "No models loaded"

    @classmethod
    def launch_ipython(cls, namespace: dict[str, Any]) -> None:
        """Runs IPython with top-level ``await`` on the event loop the Hare context was opened on.

        Args:
            namespace: The shell's namespace.
        """
        assert cls.interactive_shell_class is not None and cls.nest_asyncio is not None  # nosec B101
        cls.nest_asyncio.apply()
        banner = (
            "Hare ORM Shell (IPython with async support)\n"
            f"{cls._shell_models_info(namespace)}\n"
            "Use 'await' directly for async operations (e.g., 'await YourModel.objects.all()').\n"
        )
        with contextlib.suppress(EOFError, ValueError):
            shell = cls.interactive_shell_class(user_ns=namespace, banner1=banner)
            shell.autoawait = True
            shell()


try:
    import nest_asyncio
    from IPython.terminal.embed import InteractiveShellEmbed

    ShellLauncher.interactive_shell_class = InteractiveShellEmbed
    ShellLauncher.nest_asyncio = nest_asyncio
except ImportError:
    pass
