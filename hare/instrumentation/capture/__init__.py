"""Capturing the rows a model's writes change, in the write's own transaction -
``Meta.change_capture`` names a ``ChangeSink`` the changes go to."""

from __future__ import annotations

from hare.instrumentation.capture.captured_change import CapturedChange
from hare.instrumentation.capture.change_sink import ChangeSink

__all__ = ["CapturedChange", "ChangeSink"]
