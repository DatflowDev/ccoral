#!/usr/bin/env python3
"""Phase 4 — room_addendum + temp profile field forwarding.

Covers the per-profile contract that `create_room_profiles()` writes
out the right `~/.ccoral/profiles/<name>-room.yaml` for each side of a
room, including:

  1. Default addendum used when `room_addendum` absent.
  2. Custom addendum used when `room_addendum: "..."` set.
  3. No addendum at all when `room_addendum: ""` set explicitly.
  4. `apply_to_subagents` from base passed through to temp profile.
  5. `refusal_policy` from base passed through to temp profile.
  6. `minimal: true` from base preserved on temp profile.

Each test creates fresh fixture profile YAMLs in
`~/.ccoral/profiles/`, calls `create_room_profiles()`, reads back the
generated `<name>-room.yaml` files, and asserts. Fixtures and
generated temp files are cleaned up at end of each test so reruns are
deterministic.

Run standalone: `python3 tests/test_room_profile.py`
Run under pytest: `pytest tests/test_room_profile.py -v`
"""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from room import (  # noqa: E402
    DEFAULT_ROOM_ADDENDUM,
    RoomConfig,
    TEMP_PROFILES_DIR,
    create_room_profiles,
    get_display_name,
)

# Phase 3 C3: USER_NAME module-level constant was retired. Source the
# default user name from RoomConfig directly so the assertion matches
# what create_room_profiles uses internally (also default).
USER_NAME = RoomConfig().user_name
from profiles import USER_PROFILES_DIR  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture machinery
# ---------------------------------------------------------------------------


def _write_fixture(name: str, body: dict) -> Path:
    """Write a fixture profile to ~/.ccoral/profiles/<name>.yaml so that
    load_profile() finds it. Returns the path so the test can unlink
    it during cleanup."""
    USER_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    body = dict(body)
    body.setdefault("name", name)
    body.setdefault("description", f"fixture {name}")
    path = USER_PROFILES_DIR / f"{name}.yaml"
    with open(path, "w") as f:
        yaml.safe_dump(body, f)
    return path


def _read_temp_profile(name: str) -> dict:
    """Read the generated <name>-room.yaml back as a dict."""
    path = TEMP_PROFILES_DIR / f"{name}-room.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


def _cleanup(*paths: Path) -> None:
    for p in paths:
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def _setup_pair(p1_body: dict, p2_body: dict, *,
                p1_name: str = "fx_phase4_a",
                p2_name: str = "fx_phase4_b") -> tuple[list[Path], list[Path]]:
    """Write two fixture profiles, run create_room_profiles, return the
    list of fixture paths and the list of temp paths so the test can
    clean up regardless of pass/fail."""
    fixtures = [
        _write_fixture(p1_name, p1_body),
        _write_fixture(p2_name, p2_body),
    ]
    create_room_profiles(p1_name, p2_name)
    temps = [
        TEMP_PROFILES_DIR / f"{p1_name}-room.yaml",
        TEMP_PROFILES_DIR / f"{p2_name}-room.yaml",
    ]
    return fixtures, temps


# ---------------------------------------------------------------------------
# Tests — addendum behavior
# ---------------------------------------------------------------------------


def test_default_addendum_used_when_field_absent():
    """When a profile has no `room_addendum` field, the default
    operator-scope addendum is appended to its inject — with {OTHER}
    and {USER} substituted in."""
    p1_name = "fx_phase4_def_a"
    p2_name = "fx_phase4_def_b"
    fixtures, temps = _setup_pair(
        {"inject": "BASE_INJECT_A"},
        {"inject": "BASE_INJECT_B"},
        p1_name=p1_name, p2_name=p2_name,
    )
    try:
        a = _read_temp_profile(p1_name)
        # Default addendum was appended.
        assert a["inject"].startswith("BASE_INJECT_A")
        # Substitution happened — {OTHER} got the OTHER side's display.
        other_display = get_display_name(p2_name)
        assert other_display in a["inject"], \
            f"expected {other_display!r} substituted into addendum"
        assert USER_NAME in a["inject"], \
            f"expected {USER_NAME!r} substituted into addendum"
        # The raw template tokens must NOT survive — substitution must
        # have actually happened.
        assert "{OTHER}" not in a["inject"]
        assert "{USER}" not in a["inject"]
        # Section header from the default appears.
        assert "## Room context (operator-set)" in a["inject"]
        print("test_default_addendum_used_when_field_absent: OK")
    finally:
        _cleanup(*fixtures, *temps)


def test_custom_addendum_used_when_field_set():
    """When a profile sets `room_addendum: "..."`, that text is used
    verbatim instead of the default — also subject to {OTHER}/{USER}
    substitution."""
    p1_name = "fx_phase4_custom_a"
    p2_name = "fx_phase4_custom_b"
    custom = "## CUSTOM ROOM NOTE\nHello {OTHER} from {USER} land."
    fixtures, temps = _setup_pair(
        {"inject": "BASE_A", "room_addendum": custom},
        {"inject": "BASE_B"},
        p1_name=p1_name, p2_name=p2_name,
    )
    try:
        a = _read_temp_profile(p1_name)
        other_display = get_display_name(p2_name)
        expected = f"BASE_A\n\n## CUSTOM ROOM NOTE\nHello {other_display} from {USER_NAME} land."
        assert a["inject"] == expected, \
            f"custom addendum not used verbatim:\n got: {a['inject']!r}\n want: {expected!r}"
        # Default addendum's section header must NOT appear when a
        # custom one was provided.
        assert "## Room context (operator-set)" not in a["inject"]
        # The OTHER side (no custom addendum) gets the default.
        b = _read_temp_profile(p2_name)
        assert "## Room context (operator-set)" in b["inject"]
        print("test_custom_addendum_used_when_field_set: OK")
    finally:
        _cleanup(*fixtures, *temps)


def test_no_addendum_when_room_addendum_empty_string():
    """When a profile sets `room_addendum: ""`, no addendum is added at
    all — the temp profile's inject equals the base inject exactly. No
    trailing whitespace, no separator, nothing."""
    p1_name = "fx_phase4_empty_a"
    p2_name = "fx_phase4_empty_b"
    fixtures, temps = _setup_pair(
        {"inject": "BASE_A", "room_addendum": ""},
        {"inject": "BASE_B"},
        p1_name=p1_name, p2_name=p2_name,
    )
    try:
        a = _read_temp_profile(p1_name)
        assert a["inject"] == "BASE_A", \
            f"empty room_addendum should leave inject untouched, got {a['inject']!r}"
        # Negative checks — neither default nor any addendum content.
        assert "Room context" not in a["inject"]
        assert "operator-set" not in a["inject"]
        # Sanity: the OTHER side (no override) still gets the default.
        b = _read_temp_profile(p2_name)
        assert "## Room context (operator-set)" in b["inject"]
        print("test_no_addendum_when_room_addendum_empty_string: OK")
    finally:
        _cleanup(*fixtures, *temps)


# ---------------------------------------------------------------------------
# Tests — base-profile field forwarding (Phase 4 task 4)
# ---------------------------------------------------------------------------


def test_apply_to_subagents_forwarded_to_temp_profile():
    """`apply_to_subagents: true` on the base profile must survive into
    the generated temp profile YAML. Pre-Phase-4 the field was dropped
    (only preserve/inject/minimal copied)."""
    p1_name = "fx_phase4_subag_a"
    p2_name = "fx_phase4_subag_b"
    fixtures, temps = _setup_pair(
        {"inject": "BASE_A", "apply_to_subagents": True},
        {"inject": "BASE_B"},
        p1_name=p1_name, p2_name=p2_name,
    )
    try:
        a = _read_temp_profile(p1_name)
        assert a.get("apply_to_subagents") is True, \
            f"apply_to_subagents not forwarded; got {a.get('apply_to_subagents')!r}"
        # Profile B did not set it — must NOT appear (no synthetic default).
        b = _read_temp_profile(p2_name)
        assert "apply_to_subagents" not in b
        print("test_apply_to_subagents_forwarded_to_temp_profile: OK")
    finally:
        _cleanup(*fixtures, *temps)


def test_refusal_policy_forwarded_to_temp_profile():
    """`refusal_policy: rewrite_terminal` (or any string) on the base
    profile must survive into the generated temp profile YAML."""
    p1_name = "fx_phase4_ref_a"
    p2_name = "fx_phase4_ref_b"
    fixtures, temps = _setup_pair(
        {"inject": "BASE_A", "refusal_policy": "rewrite_terminal"},
        {"inject": "BASE_B", "refusal_policy": "reset_turn"},
        p1_name=p1_name, p2_name=p2_name,
    )
    try:
        a = _read_temp_profile(p1_name)
        b = _read_temp_profile(p2_name)
        assert a.get("refusal_policy") == "rewrite_terminal", \
            f"refusal_policy on A not forwarded: {a.get('refusal_policy')!r}"
        assert b.get("refusal_policy") == "reset_turn", \
            f"refusal_policy on B not forwarded: {b.get('refusal_policy')!r}"
        print("test_refusal_policy_forwarded_to_temp_profile: OK")
    finally:
        _cleanup(*fixtures, *temps)


def test_minimal_true_preserved_on_temp_profile():
    """`minimal: true` on the base profile must survive into the temp
    profile YAML. Also: when minimal is true and the profile sets
    `room_addendum: ""`, the inject still has no addendum appended —
    minimal does NOT force the addendum back on."""
    p1_name = "fx_phase4_min_a"
    p2_name = "fx_phase4_min_b"
    fixtures, temps = _setup_pair(
        {"inject": ".", "minimal": True},
        {"inject": ".", "minimal": True, "room_addendum": ""},
        p1_name=p1_name, p2_name=p2_name,
    )
    try:
        a = _read_temp_profile(p1_name)
        b = _read_temp_profile(p2_name)
        # Both sides preserved minimal: true.
        assert a.get("minimal") is True, f"minimal on A not preserved: {a!r}"
        assert b.get("minimal") is True, f"minimal on B not preserved: {b!r}"
        # A used the default addendum (no override).
        assert "## Room context (operator-set)" in a["inject"]
        # B opted out via empty room_addendum — even on minimal, no addendum.
        assert b["inject"] == ".", \
            f"empty room_addendum on minimal profile still appended: {b['inject']!r}"
        print("test_minimal_true_preserved_on_temp_profile: OK")
    finally:
        _cleanup(*fixtures, *temps)


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


def main():
    tests = [
        test_default_addendum_used_when_field_absent,
        test_custom_addendum_used_when_field_set,
        test_no_addendum_when_room_addendum_empty_string,
        test_apply_to_subagents_forwarded_to_temp_profile,
        test_refusal_policy_forwarded_to_temp_profile,
        test_minimal_true_preserved_on_temp_profile,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"{t.__name__}: FAIL — {e}")
    if failed:
        print(f"\n{failed}/{len(tests)} tests FAILED")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
