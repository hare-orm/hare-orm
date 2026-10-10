"""The mypy plugin of hare - ``plugins = ["hare.contrib.mypy"]`` in mypy's configuration. Needs the
``mypy`` extra (``pip install hare-orm[mypy]``)."""

from __future__ import annotations

from hare.contrib.mypy.hare_mypy_plugin import HareMypyPlugin

#: What mypy calls with its version to get the plugin class.
plugin = HareMypyPlugin.for_version

__all__ = ["HareMypyPlugin", "plugin"]
