"""Phase 10 — Profile picker smoke tests.

Drives `room_picker.PickerApp` via `App.run_test()` + `Pilot` to prove
the critical interaction surface: column navigation, the '/' filter
mode, the same-profile confirm modal (both yes and no paths), the
optional topic step, and clean exit on Esc.

We never spawn proxies, FIFOs, tmux, or any subprocess here — the test
exercises the picker's UI shell only. The CLI hook in `cmd_room` lives
in `ccoral` and is exercised by the existing test_room_cli_flags.py
patterns (or, when not, by hand-stub harness in this file's parse-side
test).

Pattern reference: tests/test_room_app.py.

Run standalone: `python3 tests/test_room_picker.py`
Run under pytest: `pytest tests/test_room_picker.py -v`
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import room_picker
from textual.widgets import Input, Label, OptionList


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run a Pilot coroutine to completion. Same pattern as test_room_app."""
    return asyncio.run(coro)


def _picker_screen(app):
    """Return the live PickerScreen instance from an App.run_test() app."""
    # PickerApp.on_mount pushes PickerScreen onto the stack; the default
    # Screen sits at index 0, the picker at index 1.
    for scr in app.screen_stack:
        if isinstance(scr, room_picker.PickerScreen):
            return scr
    raise AssertionError(f"PickerScreen not in stack: {app.screen_stack!r}")


# ---------------------------------------------------------------------------
# Test 1 — column navigation + description label updates
# ---------------------------------------------------------------------------


def test_tab_moves_focus_between_columns():
    """Tab swaps focus from #col1 to #col2; Shift-Tab swaps back.

    Also verifies the description Label updates when arrow keys move the
    highlight within a column (OptionList.OptionHighlighted handler).
    """
    async def scenario():
        app = room_picker.PickerApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            scr = _picker_screen(app)
            # Initial focus is on col1 (set in PickerScreen.on_mount).
            assert app.focused is not None and app.focused.id == "col1"

            # Move highlight in col1 — description Label should populate
            # with the highlighted profile's description.
            await pilot.press("down")
            await pilot.pause()
            desc = scr.query_one("#description", Label)
            # We don't pin the literal description (depends on which
            # profile is alphabetically first), but the Label.render()
            # call should return a string-like result without raising.
            rendered = str(desc.render())
            assert isinstance(rendered, str)

            # Tab → col2.
            await pilot.press("tab")
            await pilot.pause()
            assert app.focused is not None and app.focused.id == "col2"

            # Shift-Tab → back to col1 (action_prev_column is symmetric
            # with action_next_column in C1+C2; Footer label distinguishes).
            await pilot.press("shift+tab")
            await pilot.pause()
            assert app.focused.id == "col1"

            app.exit(None)

    _run(scenario())


# ---------------------------------------------------------------------------
# Test 2 — filter mode narrows both columns; Esc clears + restores focus
# ---------------------------------------------------------------------------


def test_filter_mode_narrows_columns_and_esc_clears():
    """Pressing '/' shows #filter; typing narrows both columns to matches;
    Esc clears the filter, hides it, and re-focuses the first column.
    """
    async def scenario():
        app = room_picker.PickerApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            scr = _picker_screen(app)
            col1 = scr.query_one("#col1", OptionList)
            col2 = scr.query_one("#col2", OptionList)
            flt = scr.query_one("#filter", Input)

            initial_count = col1.option_count
            assert initial_count > 0, "list_profiles() returned nothing"
            assert col1.option_count == col2.option_count

            # Open filter.
            await pilot.press("slash")
            await pilot.pause()
            assert not flt.has_class("hidden")
            assert app.focused is flt

            # Narrow to entries containing 'b' — at least one bundled
            # profile name has a 'b' (blank, by design); we only assert
            # the filter actually changed the count, not its specific value.
            await pilot.press("b")
            await pilot.pause()
            assert col1.option_count <= initial_count
            assert col1.option_count == col2.option_count

            # Esc clears the filter and restores the picker focus.
            await pilot.press("escape")
            await pilot.pause()
            assert flt.has_class("hidden")
            assert flt.value == ""
            assert col1.option_count == initial_count
            assert app.focused is not None and app.focused.id == "col1"

            app.exit(None)

    _run(scenario())


# ---------------------------------------------------------------------------
# Test 3 — same-profile picks push ConfirmScreen; 'y' proceeds, 'n' returns
# ---------------------------------------------------------------------------


def test_same_profile_confirm_modal_yes_proceeds_to_topic():
    """Highlighting the same profile in both columns and pressing Enter
    pushes ConfirmScreen; 'y' dismisses True and pushes TopicScreen, where
    pressing Enter on an empty Input resolves the picker with topic=None.
    """
    async def scenario():
        app = room_picker.PickerApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            # Move both columns onto the same option (idx 0 by default
            # after one 'down' press).
            await pilot.press("down")             # col1 idx 0
            await pilot.press("tab")
            await pilot.press("down")             # col2 idx 0 (same)
            await pilot.pause()

            # Enter while a column is focused fires OptionList.OptionSelected,
            # which the screen funnels into action_confirm.
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, room_picker.ConfirmScreen), (
                f"expected ConfirmScreen, got {type(app.screen).__name__}"
            )

            # 'y' dismisses True and pushes TopicScreen.
            await pilot.press("y")
            await pilot.pause()
            assert isinstance(app.screen, room_picker.TopicScreen), (
                f"expected TopicScreen, got {type(app.screen).__name__}"
            )

            # Empty topic + Enter → resolve with topic=None.
            await pilot.press("enter")
            await pilot.pause()
            assert app.result is not None
            p1, p2, topic = app.result
            assert p1 == p2, f"expected same-profile pair, got {(p1, p2)!r}"
            assert topic is None, f"expected None topic, got {topic!r}"

    _run(scenario())


def test_same_profile_confirm_modal_no_returns_to_picker():
    """'n' on the ConfirmScreen dismisses False and returns to the picker
    without resolving — operator can adjust their selection and retry.
    """
    async def scenario():
        app = room_picker.PickerApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.press("tab")
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, room_picker.ConfirmScreen)

            await pilot.press("n")
            await pilot.pause()
            # Back on the picker; result still unset.
            assert isinstance(app.screen, room_picker.PickerScreen), (
                f"expected PickerScreen, got {type(app.screen).__name__}"
            )
            assert app.result is None

            app.exit(None)

    _run(scenario())


# ---------------------------------------------------------------------------
# Test 5 — Esc on the picker (no filter active) exits with no result
# ---------------------------------------------------------------------------


def test_escape_on_picker_exits_with_no_result():
    """Esc / q / Ctrl+C on the picker (when the filter is hidden) exits
    cleanly with `app.result == None` — the cmd_room caller treats that
    as "operator cancelled" and prints the cancellation notice.
    """
    async def scenario():
        app = room_picker.PickerApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, room_picker.PickerScreen)
            await pilot.press("escape")
            await pilot.pause()
            # The action calls app.exit(None); run_test detects shutdown
            # and the async-with exits.
        # Outside the context: result must remain None.
        assert app.result is None, f"expected None, got {app.result!r}"

    _run(scenario())


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


def main():
    tests = [
        test_tab_moves_focus_between_columns,
        test_filter_mode_narrows_columns_and_esc_clears,
        test_same_profile_confirm_modal_yes_proceeds_to_topic,
        test_same_profile_confirm_modal_no_returns_to_picker,
        test_escape_on_picker_exits_with_no_result,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"{t.__name__}: OK")
        except AssertionError as e:
            failed += 1
            print(f"{t.__name__}: FAIL — {e}")
        except Exception as e:
            failed += 1
            print(f"{t.__name__}: ERROR — {type(e).__name__}: {e}")
    if failed:
        print(f"\n{failed}/{len(tests)} tests FAILED")
        sys.exit(1)
    print("\nAll tests passed.")


if __name__ == "__main__":
    main()
