"""The site's one "page not found" page, in English."""

from __future__ import annotations

from typing import Any

from mkdocs_hooks.constants import NOT_FOUND_PAGE_LANGUAGE, NOT_FOUND_TEMPLATE_NAME


class NotFoundPage:
    """Keeps the root ``404.html`` the English build writes: the i18n plugin builds the site once per
    language, each build writing the theme's ``404.html`` to the root, and the last - the Russian one -
    would otherwise be the page every missing path shows."""

    @staticmethod
    def on_post_template(output_content: str, template_name: str, config: Any, **kwargs: Any) -> str:
        """Drops the page of a build in another language - mkdocs writes no empty template.

        Args:
            output_content: The rendered template.
            template_name: The template's name.
            config: The site's configuration.
            kwargs: The rest of the event's arguments.

        Returns:
            The rendered template, empty for another language's ``404.html``.
        """
        if template_name == NOT_FOUND_TEMPLATE_NAME and config["theme"]["language"] != NOT_FOUND_PAGE_LANGUAGE:
            return ""
        return output_content


# The name mkdocs calls the hook by.
on_post_template = NotFoundPage.on_post_template
