from __future__ import annotations

from collections.abc import Mapping

from hare.migrations.autodetection.rename_questions import RenameQuestions


class InteractiveRenameQuestions(RenameQuestions):
    """The rename questions of ``hare makemigrations``: an answer given with ``--rename`` decides, any
    other is asked in the terminal - or no, without one (``--no-input``, input not from a terminal).

    Args:
        renames: The answers given with ``--rename``.
        asks: Whether to ask in the terminal.
    """

    def __init__(self, renames: Mapping[str, str] | None = None, *, asks: bool) -> None:
        super().__init__(renames)
        self.asks = asks

    def ask(self, question: str) -> bool:
        if not self.asks:
            return False
        try:
            answer = input(f"{question} [y/N] ")
        except EOFError:
            # The input ended (Ctrl-D, a null device Windows reports as a terminal) - no answer is a no.
            print()
            return False
        return answer.strip().lower() in ("y", "yes")
