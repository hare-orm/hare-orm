"""The titles of the admonitions GitHub alerts (``> [!WARNING]``) render as on the site."""

from __future__ import annotations

from typing import Any

from mkdocs_hooks.constants import RUSSIAN_CALLOUT_TITLES, TITLED_CALLOUT, UNTITLED_CALLOUT


class CalloutTitles:
    """Gives each admonition a GitHub alert made the title its bold first line holds - an alert has no
    title of its own - and an untitled one on a Russian page a Russian title."""

    @staticmethod
    def on_page_content(html: str, page: Any, **kwargs: Any) -> str:
        """Retitles the page's admonitions.

        Args:
            html: The page's HTML.
            page: The page.
            kwargs: The rest of the event's arguments.

        Returns:
            The HTML.
        """
        html = TITLED_CALLOUT.sub(r'\1<p class="admonition-title">\g<title></p>', html)
        if page.file.src_uri.endswith(".ru.md"):
            html = UNTITLED_CALLOUT.sub(
                lambda match: f'<p class="admonition-title">{RUSSIAN_CALLOUT_TITLES[match[1]]}</p>', html
            )
        return html


# The name mkdocs calls the hook by.
on_page_content = CalloutTitles.on_page_content
