"""res:// resolution, shared by the Godot extractors (.gd, .gdshader, .tscn).

Godot addresses every file it loads as ``res://…``, resolved against the directory
holding project.godot. A script, a shader and a scene all use the same scheme, so
they resolve it the same way here.
"""
from __future__ import annotations

from pathlib import Path

# Walking up from every file would stat the same ancestors once per file, so the
# answer is cached for each directory seen on the way.
_GODOT_ROOT_CACHE: dict[str, Path | None] = {}


def godot_root(path: Path) -> Path | None:
    """Directory holding project.godot at or above ``path``, or None."""
    start = path.parent
    key = str(start)
    if key in _GODOT_ROOT_CACHE:
        return _GODOT_ROOT_CACHE[key]

    root: Path | None = None
    probe = start
    seen: list[str] = []
    # A scan can be handed relative paths, so stop on a parent that no longer
    # changes as well as on the filesystem root — Path("a").parent is ".", and
    # "." is its own parent forever.
    for _ in range(64):
        seen.append(str(probe))
        try:
            if (probe / "project.godot").is_file():
                root = probe
                break
        except OSError:
            break
        parent = probe.parent
        if parent == probe:
            break
        probe = parent

    for key_seen in seen:
        _GODOT_ROOT_CACHE[key_seen] = root
    return root


def resolve_res_path(raw: str, path: Path) -> Path | None:
    """Map a ``res://…`` literal to a file path in the same style as ``path``.

    Returns None when the target does not exist: a missing path would otherwise
    become a node standing for a file nobody can open.
    """
    if not raw.startswith("res://"):
        return None
    root = godot_root(path)
    if root is None:
        return None
    target = root / raw[len("res://"):]
    try:
        if not target.is_file():
            return None
    except OSError:
        return None
    return target
