"""
CCORAL v2 — Profile Manager
=============================

Loads YAML profiles and watches for changes.
"""

import yaml
from pathlib import Path
from typing import Optional


PROFILES_DIR = Path(__file__).resolve().parent / "profiles"
USER_PROFILES_DIR = Path.home() / ".ccoral" / "profiles"


def _search_dirs() -> list[Path]:
    """Profile search directories, user first."""
    dirs = []
    if USER_PROFILES_DIR.exists():
        dirs.append(USER_PROFILES_DIR)
    if PROFILES_DIR.exists():
        dirs.append(PROFILES_DIR)
    return dirs


def list_profiles() -> list[dict]:
    """List all available profiles."""
    seen = set()
    profiles = []
    for d in _search_dirs():
        for f in sorted(d.glob("*.yaml")):
            name = f.stem
            if name in seen:
                continue
            seen.add(name)
            try:
                data = yaml.safe_load(f.read_text())
                profiles.append({
                    "name": name,
                    "description": data.get("description", ""),
                    "path": str(f),
                })
            except Exception:
                profiles.append({
                    "name": name,
                    "description": "(error loading)",
                    "path": str(f),
                })
    return profiles


def load_profile(name: str) -> Optional[dict]:
    """Load a profile by name."""
    for d in _search_dirs():
        path = d / f"{name}.yaml"
        if path.exists():
            data = yaml.safe_load(path.read_text())
            data["_path"] = str(path)
            data["_name"] = name
            return data
    return None


def _active_profile_path(port: Optional[int]) -> Path:
    """Return the file path that holds the active profile name.

    With no port: the global ``~/.ccoral/active_profile`` file.
    With a port: the per-port ``~/.ccoral/active_profile.<port>`` file
    (used to scope active-profile state to one daemon when multiple
    ccoral instances are running on different ports).
    """
    base = Path.home() / ".ccoral"
    if port is not None:
        return base / f"active_profile.{port}"
    return base / "active_profile"


def get_active_profile(port: Optional[int] = None) -> Optional[str]:
    """Get the currently active profile name.

    Resolution order:
        1. If ``port`` is given AND ``~/.ccoral/active_profile.<port>``
           exists and is non-empty, return its contents.
        2. Otherwise fall back to the global ``~/.ccoral/active_profile``.
        3. Return None if neither is set.
    """
    if port is not None:
        per_port = _active_profile_path(port)
        if per_port.exists():
            name = per_port.read_text().strip()
            if name:
                return name
            # File exists but empty — fall through to global rather than
            # returning None, so an empty per-port file behaves like "unset".

    global_file = _active_profile_path(None)
    if global_file.exists():
        name = global_file.read_text().strip()
        return name if name else None
    return None


def set_active_profile(name: Optional[str], port: Optional[int] = None):
    """Set or unset the active profile.

    With no port: writes to (or removes) the global ``active_profile`` file.
    With a port: writes to (or removes) the per-port file only — leaves the
    global file untouched.

    Pass ``name=None`` to unset.
    """
    target = _active_profile_path(port)
    target.parent.mkdir(parents=True, exist_ok=True)
    if name:
        target.write_text(name)
    elif target.exists():
        target.unlink()


def load_active_profile(port: Optional[int] = None) -> Optional[dict]:
    """Load the currently active profile (port-aware)."""
    name = get_active_profile(port=port)
    if name:
        return load_profile(name)
    return None
