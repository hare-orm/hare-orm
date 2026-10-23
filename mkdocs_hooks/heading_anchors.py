"""The heading anchors of the docs pages, written for GitHub, as the site's own heading ids."""

from __future__ import annotations

from typing import Any

from mkdocs_hooks.constants import FENCED_CODE, HTML_ANCHORED_HEADING


class HeadingAnchors:
    """Turns ``## <a id="anchor"></a>Title`` - an anchor GitHub shows nothing of - into
    ``## Title {: #anchor }``, so the site gives the heading itself that id, in its table of
    contents and permalink too."""

    @staticmethod
    def on_page_markdown(markdown: str, page: Any, **kwargs: Any) -> str:
        """Rewrites the page's anchored headings, outside its fenced code.

        Args:
            markdown: The page's Markdown.
            page: The page.
            kwargs: The rest of the event's arguments.

        Returns:
            The Markdown.
        """
        parts = FENCED_CODE.split(markdown)
        for index, part in enumerate(parts):
            if not FENCED_CODE.fullmatch(part):
                parts[index] = HTML_ANCHORED_HEADING.sub(r"\g<level> \g<title> {: #\g<anchor> }", part)
        return "".join(parts)


# The name mkdocs calls the hook by.
on_page_markdown = HeadingAnchors.on_page_markdown
