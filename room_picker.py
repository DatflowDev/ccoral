"""
CCORAL Room — Profile Picker (Phase 10)
========================================

Textual `App` that lets the operator pick two profiles for a room before
`run_room` is invoked. Used when `ccoral room` is called without two
positional profile names (and `--no-picker` was not passed).

Public surface (consumed by `ccoral`'s `cmd_room`):

    pick_profiles(initial_topic: str | None = None)
        -> tuple[str, str, str | None] | None

      Runs the picker App synchronously and returns ``(p1, p2, topic)`` once
      the operator confirms — or ``None`` if they cancelled (Esc / Ctrl+C / q).
      ``initial_topic`` pre-fills the topic prompt so callers can pass a
      single positional that didn't match a profile name.

Layout (PickerScreen):
  - Header
  - Horizontal: two OptionList columns (slot 1 / slot 2) populated from
    `profiles.list_profiles()`.
  - Description label updated on OptionList.OptionHighlighted.
  - Hidden Input ('#filter'): toggled by '/'; on each Input.Changed, both
    columns are re-populated with matching profile entries.
  - Footer (BINDINGS render here via show=True).

C1 lands the picker shell + filter; the same-profile guard surfaces a
description warning and refuses to proceed. C2 will replace that with the
ConfirmScreen modal and land the optional TopicScreen step. Until C2,
distinct picks resolve immediately with topic=None.

References (verified against installed textual==8.2.5 — see Phase 9
notes in room_app.py and the introspection log in this phase's commit):

  - Screen:                 https://textual.textualize.io/api/screen/
  - OptionList + Option:    https://textual.textualize.io/widgets/option_list
  - Input.Changed:          https://textual.textualize.io/widgets/input
  - Binding(show=True):     https://textual.textualize.io/guide/input
"""

from __future__ import annotations

from typing import Callable

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Label, OptionList
from textual.widgets.option_list import Option

import profiles


# ---------------------------------------------------------------------------
# PickerScreen — the two-column profile chooser.
# ---------------------------------------------------------------------------


class PickerScreen(Screen):
    """Two-column profile picker. Resolves to (p1, p2, topic) via the App."""

    BINDINGS = [
        Binding("tab", "next_column", "Next column", show=True),
        Binding("shift+tab", "prev_column", "Prev column"),
        Binding("slash", "filter", "Filter", show=True),
        Binding("enter", "confirm", "Launch", show=True),
        Binding("ctrl+c,q,escape", "cancel", "Quit", show=True, priority=True),
    ]

    def __init__(self, initial_topic: str | None = None) -> None:
        super().__init__()
        # Cached, alphabetised profile list. We hold the raw list so the
        # filter handler can re-derive Options without re-reading from disk
        # on every keystroke.
        self._all_profiles: list[dict] = sorted(
            profiles.list_profiles(),
            key=lambda p: p["name"].lower(),
        )
        # Map profile name → description for the on_highlight callback.
        self._descriptions: dict[str, str] = {
            p["name"]: (p.get("description") or "") for p in self._all_profiles
        }
        # Carried into the topic step so a single positional pre-fills it.
        self._initial_topic = initial_topic

    # ─── compose ────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(name="ccoral room — pick two profiles")
        with Horizontal(id="columns"):
            yield OptionList(*self._make_options(), id="col1")
            yield OptionList(*self._make_options(), id="col2")
        yield Label("", id="description")
        yield Input(id="filter", placeholder="/ to filter", classes="hidden")
        yield Footer()

    def on_mount(self) -> None:
        # Focus the first column so arrow keys work immediately.
        self.query_one("#col1", OptionList).focus()

    # ─── data helpers ──────────────────────────────────────────────────

    def _make_options(self, query: str = "") -> list[Option]:
        """Return Option objects for the current filter query.

        Empty query returns every profile. Match is a case-insensitive
        substring against the profile name (description not searched —
        names are short and operator-typed; matching descriptions would
        cause noisy hits like "thoughtful" matching every profile).
        """
        q = query.strip().lower()
        out: list[Option] = []
        for p in self._all_profiles:
            name = p["name"]
            if q and q not in name.lower():
                continue
            out.append(Option(name, id=name))
        return out

    def _refresh_columns(self, query: str = "") -> None:
        new_options = self._make_options(query)
        for col_id in ("#col1", "#col2"):
            col = self.query_one(col_id, OptionList)
            col.clear_options()
            col.add_options(new_options)

    # ─── event handlers ────────────────────────────────────────────────

    @on(OptionList.OptionHighlighted)
    def on_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """Update the description label when focus moves within either column."""
        name = event.option_id or ""
        desc = self._descriptions.get(name, "")
        self.query_one("#description", Label).update(desc)

    @on(Input.Changed, "#filter")
    def on_filter_changed(self, event: Input.Changed) -> None:
        self._refresh_columns(event.value)

    @on(Input.Submitted, "#filter")
    def on_filter_submit(self, event: Input.Submitted) -> None:
        # Enter inside the filter Input commits the filter and re-focuses
        # the first column. Esc (action_cancel below in column context)
        # clears it.
        self._hide_filter(clear=False)

    # ─── actions ───────────────────────────────────────────────────────

    def action_next_column(self) -> None:
        focused = self.focused
        target = "#col2" if focused is None or focused.id == "col1" else "#col1"
        self.query_one(target, OptionList).focus()

    def action_prev_column(self) -> None:
        # Symmetric — Tab and Shift-Tab both swap, but kept distinct for
        # the binding label in the Footer.
        self.action_next_column()

    def action_filter(self) -> None:
        """Show the filter Input and focus it."""
        flt = self.query_one("#filter", Input)
        flt.remove_class("hidden")
        flt.value = ""
        flt.focus()

    def action_cancel(self) -> None:
        """Esc / q / Ctrl+C — context-aware:
        - if filter is visible, hide it and re-focus the first column;
        - else, exit the picker (App.exit with no result).
        """
        flt = self.query_one("#filter", Input)
        if not flt.has_class("hidden"):
            self._hide_filter(clear=True)
            return
        # No active filter → quit the App entirely.
        self.app.exit(None)

    def _hide_filter(self, *, clear: bool) -> None:
        flt = self.query_one("#filter", Input)
        if clear:
            flt.value = ""
            self._refresh_columns("")
        flt.add_class("hidden")
        self.query_one("#col1", OptionList).focus()

    def action_confirm(self) -> None:
        """Enter — read both columns' highlighted options and resolve.

        C1: distinct picks resolve immediately with topic=None. Same-name
        picks are blocked with a description-bar warning and require the
        operator to change one column before retrying. C2 replaces that
        block with a ConfirmScreen modal and adds the optional topic step.
        """
        col1 = self.query_one("#col1", OptionList)
        col2 = self.query_one("#col2", OptionList)
        p1 = self._highlighted_id(col1)
        p2 = self._highlighted_id(col2)
        if not p1 or not p2:
            # Nothing highlighted in one of the columns — surface in the
            # description bar; don't crash, don't beep.
            self.query_one("#description", Label).update(
                "Highlight a profile in each column before pressing Enter.",
            )
            return

        if p1 == p2:
            # C1 placeholder behaviour — a same-profile pick is refused
            # here so we never accidentally resolve to (name, name) before
            # the C2 confirm modal lands.
            self.query_one("#description", Label).update(
                f"Same profile in both columns ({p1!r}). Pick distinct "
                "profiles, or wait for the same-profile confirm modal.",
            )
            return

        self._finish(p1, p2, self._initial_topic)

    @staticmethod
    def _highlighted_id(col: OptionList) -> str | None:
        """Return the highlighted option's id, or None if nothing focused."""
        idx = col.highlighted
        if idx is None:
            return None
        try:
            return col.get_option_at_index(idx).id
        except Exception:
            return None

    def _finish(self, p1: str, p2: str, topic: str | None) -> None:
        """Hand the resolved tuple to the App via its result attribute."""
        app = self.app
        if isinstance(app, PickerApp):
            app.result = (p1, p2, topic)
        app.exit(0)


# ---------------------------------------------------------------------------
# PickerApp — thin App wrapper. Standalone so callers don't have to push
# the picker onto an existing App; cleaner than embedding inside RoomApp.
# ---------------------------------------------------------------------------


class PickerApp(App):
    """Standalone App that hosts PickerScreen and exposes the picked tuple.

    The resolved (p1, p2, topic) lives on `self.result`; `pick_profiles()`
    reads it after `run()` returns.
    """

    CSS_PATH = "room_picker.tcss"

    def __init__(self, initial_topic: str | None = None) -> None:
        super().__init__()
        self._initial_topic = initial_topic
        self.result: tuple[str, str, str | None] | None = None

    def on_mount(self) -> None:
        self.push_screen(PickerScreen(initial_topic=self._initial_topic))


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def pick_profiles(
    initial_topic: str | None = None,
    *,
    _app_factory: Callable[[str | None], PickerApp] | None = None,
) -> tuple[str, str, str | None] | None:
    """Launch the picker and return the resolved (p1, p2, topic) or None.

    Synchronous from the caller's perspective — Textual owns the terminal
    while the picker runs and returns control on dismiss/exit. Returns
    None when the operator cancelled (Esc / Ctrl+C / q without picking).

    The optional ``_app_factory`` hook lets tests inject a stub App; the
    real call site in `ccoral` always uses the default.
    """
    factory = _app_factory or (lambda t: PickerApp(initial_topic=t))
    app = factory(initial_topic)
    app.run()
    return app.result
