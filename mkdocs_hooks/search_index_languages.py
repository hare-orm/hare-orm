"""A search index of each language's own pages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mkdocs.plugins import event_priority

from mkdocs_hooks.constants import PAGE_CONFIG_BASE, SEARCH_INDEX_PATH


class SearchIndexLanguages:
    """Splits the one search index the i18n plugin builds for every language: the English pages keep
    ``search/search_index.json`` of their own entries, a translation's pages get ``<locale>/search/
    search_index.json`` of theirs, and their theme configuration's base points to ``<locale>/``, where
    the theme reads the index and the results' locations from."""

    @staticmethod
    def on_post_build(config: Any, **kwargs: Any) -> None:
        """Splits the site's search index by language - once the i18n plugin has written the whole one.

        Args:
            config: The site's configuration.
            kwargs: The rest of the event's arguments.
        """
        site_directory = Path(config["site_dir"])
        index_path = site_directory / SEARCH_INDEX_PATH
        if not index_path.exists():
            return
        index = json.loads(index_path.read_text(encoding="utf-8"))
        for locale in SearchIndexLanguages.get_translated_locales(config):
            prefix = f"{locale}/"
            translated_documents = [document for document in index["docs"] if document["location"].startswith(prefix)]
            if not translated_documents:
                # A language's own build, before the i18n plugin merges the indexes - or one already split.
                continue
            index["docs"] = [document for document in index["docs"] if not document["location"].startswith(prefix)]
            translated_index = {
                **index,
                "docs": [
                    {**document, "location": document["location"].removeprefix(prefix)}
                    for document in translated_documents
                ],
            }
            translated_index_path = site_directory / locale / SEARCH_INDEX_PATH
            translated_index_path.parent.mkdir(parents=True, exist_ok=True)
            translated_index_path.write_text(json.dumps(translated_index, ensure_ascii=False), encoding="utf-8")
            for page_path in (site_directory / locale).rglob("*.html"):
                SearchIndexLanguages.point_base_to_locale(page_path, locale)
        index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def get_translated_locales(config: Any) -> list[str]:
        """The locales the i18n plugin builds besides the default one.

        Args:
            config: The site's configuration.

        Returns:
            The locales.
        """
        i18n = config["plugins"]["i18n"]
        return [language.locale for language in i18n.config.languages if language.build and not language.default]

    @staticmethod
    def point_base_to_locale(page_path: Path, locale: str) -> None:
        """Points the base of a translated page's theme configuration to its language's folder.

        Args:
            page_path: The page.
            locale: Its language.
        """
        text = page_path.read_text(encoding="utf-8")
        # With the closing slash the theme resolves the index and the locations inside the folder.
        suffix = f"/{locale}/"
        pointed_text = PAGE_CONFIG_BASE.sub(
            lambda match: (
                match.group(0)
                if match.group(2).endswith(suffix)
                else f"{match.group(1)}{match.group(2)}{suffix}{match.group(3)}"
            ),
            text,
            count=1,
        )
        if pointed_text != text:
            page_path.write_text(pointed_text, encoding="utf-8")


# The name mkdocs calls the hook by - after the i18n plugin's own (-100), which writes the whole index.
on_post_build = event_priority(-200)(SearchIndexLanguages.on_post_build)
