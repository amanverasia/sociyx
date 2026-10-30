"""Compatibility shim: the browse/selection model now lives in the UI-neutral sociyx.service module."""

from __future__ import annotations

from ..service import GROUP_KEYS, BrowseModel, Group, group_of, one_line, plan_order

__all__ = ["GROUP_KEYS", "BrowseModel", "Group", "group_of", "one_line", "plan_order"]
