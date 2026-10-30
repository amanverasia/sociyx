"""Guided terminal UI (Textual). Imported lazily: other commands never pay for Textual."""

from __future__ import annotations


def run_tui(demo: bool = False) -> int:
    """Open the guided UI. In demo mode the caller has already pointed SOCIYX_HOME at a scratch dir."""
    import logging

    from .app import SociyxApp

    # Library log lines written to stderr would scribble over the screen; errors reach the user as notifications.
    logging.getLogger().handlers[:] = [logging.NullHandler()]
    SociyxApp(demo=demo).run()
    return 0
