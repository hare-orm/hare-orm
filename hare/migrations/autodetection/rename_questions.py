from __future__ import annotations

from collections.abc import Mapping


class RenameQuestions:
    """The answers to what ``makemigrations`` can't tell from the models alone: whether an added field,
    model or app's model is a removed one renamed or moved, not a new one. Asked only of a candidate - a
    removed and an added one declared alike. An answer given beforehand (``renames``) decides; any other
    is ``ask()``'s - no by default, a terminal's question in ``hare makemigrations``.

    Args:
        renames: The renames and moves, by what is renamed: ``"app.Model.old_field": "new_field"``,
            ``"app.OldModel": "NewModel"``, ``"old_app.Model": "new_app.Model"``.
    """

    def __init__(self, renames: Mapping[str, str] | None = None) -> None:
        self.renames = dict(renames or {})
        #: The answers ``ask()`` gave, by (what is renamed, what to) - each question is put once.
        self.asked_answers: dict[tuple[str, str], bool] = {}
        #: What the answers given beforehand renamed - one never asked about renamed nothing.
        self.applied_renames: set[str] = set()

    def ask_field_rename(self, app_label: str, model_name: str, old_name: str, new_name: str) -> bool:
        """Whether a removed field of a model is renamed to an added one.

        Args:
            app_label: The model's app.
            model_name: The model's name.
            old_name: The removed field.
            new_name: The added field.
        """
        return self.get_answer(
            f"{app_label}.{model_name}.{old_name}",
            new_name,
            f"Was field {old_name!r} of {app_label}.{model_name} renamed to {new_name!r}?",
        )

    def ask_model_rename(self, app_label: str, old_name: str, new_name: str) -> bool:
        """Whether a removed model of an app is renamed to an added one.

        Args:
            app_label: The app.
            old_name: The removed model.
            new_name: The added model.
        """
        return self.get_answer(
            f"{app_label}.{old_name}", new_name, f"Was model {app_label}.{old_name} renamed to {new_name}?"
        )

    def ask_model_move(self, old_label: str, new_label: str) -> bool:
        """Whether a removed model is moved to another app, keeping its table.

        Args:
            old_label: The removed model, ``app.Model``.
            new_label: The added model of the other app, ``app.Model``.
        """
        return self.get_answer(old_label, new_label, f"Was model {old_label} moved to {new_label}, keeping its table?")

    def get_answer(self, renamed: str, renamed_to: str, question: str) -> bool:
        """The answer given beforehand for what is renamed, else ``ask()``'s - asked once.

        Args:
            renamed: What may be renamed, as ``renames`` names it.
            renamed_to: What it may be renamed to.
            question: The question ``ask()`` puts.
        """
        given = self.renames.get(renamed)
        if given is not None:
            if given == renamed_to:
                self.applied_renames.add(renamed)
            return given == renamed_to
        if (renamed, renamed_to) not in self.asked_answers:
            self.asked_answers[renamed, renamed_to] = self.ask(question)
        return self.asked_answers[renamed, renamed_to]

    def get_unapplied_rename_warnings(self) -> list[str]:
        """A warning of each answer given beforehand that renamed nothing - a misspelt name, or a rename
        no candidate stands for: a model renamed while its fields change, two names swapped.

        Returns:
            The warnings.
        """
        return [
            f"The rename {renamed}={renamed_to} renamed nothing - no removed field or model and added one "
            "declared alike match it. A model renamed while its fields change is renamed in one run and "
            "changed in the next."
            for renamed, renamed_to in self.renames.items()
            if renamed not in self.applied_renames
        ]

    def ask(self, question: str) -> bool:
        """The answer to a question no answer was given beforehand for - no.

        Args:
            question: The question.
        """
        return False
