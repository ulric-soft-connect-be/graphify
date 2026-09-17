"""Godot project file extractor (project.godot).

This is the file Godot reads before anything else, and the entry points of the
whole project live in it. ``[autoload]`` names the scripts loaded as singletons,
which every other script then reaches by name alone — nothing in the project
*references* those files, so without this they sit in the graph with no inbound
edge at all, looking unused while in fact being global state. ``run/main_scene``
names the scene the game boots into, the root of the scene tree. Every other
setting holding a ``res://`` path — the window icon, the default environment, a
custom theme — is a project-level dependency of the same kind, and is read the
same way rather than by enumerating the keys Godot happens to define today.
"""
from __future__ import annotations

import re

from pathlib import Path

from graphify.extractors.base import _make_id
from graphify.extractors.godot_paths import resolve_res_path

_SECTION_RE = re.compile(r'^\[([^\]\s]+)\]$')
# A ConfigFile assignment with a string value. Keys carry slashes
# (`run/main_scene`), and only a string value can hold a res:// path — the
# numbers, booleans and `Vector2(…)` this file is otherwise full of cannot.
_KV_RE = re.compile(r'^([\w/.\-]+)\s*=\s*"([^"]*)"$')


def extract_godot_project(path: Path) -> dict:
    """Extract autoloads, the main scene, and every other res:// setting."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"nodes": [], "edges": [], "error": f"cannot read {path}"}

    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    seen_targets: set[str] = set()

    def add_node(nid: str, label: str, source_file: str, line: int | None) -> None:
        if nid in seen_ids:
            return
        seen_ids.add(nid)
        nodes.append({"id": nid, "label": label, "file_type": "code",
                      "source_file": source_file,
                      "source_location": f"L{line}" if line else ""})

    file_nid = _make_id(str_path)
    add_node(file_nid, path.name, str_path, 1)

    section = ""
    for lineno, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        header = _SECTION_RE.match(stripped)
        if header is not None:
            section = header.group(1)
            continue
        assignment = _KV_RE.match(stripped)
        if assignment is None:
            continue
        key, value = assignment.group(1), assignment.group(2)
        # An autoload path carries a leading `*` when the singleton is enabled;
        # the file it names is the same either way.
        if value.startswith("*"):
            value = value[1:]
        target = resolve_res_path(value, path)
        if target is None:
            continue
        tgt_nid = _make_id(str(target))
        add_node(tgt_nid, target.name, str(target), None)
        if tgt_nid in seen_targets:
            continue
        seen_targets.add(tgt_nid)
        edges.append({
            "source": file_nid, "target": tgt_nid, "relation": "imports_from",
            "confidence": "EXTRACTED", "source_file": str_path,
            "source_location": f"L{lineno}", "weight": 1.0,
            # In [autoload] the key IS the global name scripts use, so it is the
            # part of the setting worth carrying into the graph.
            "context": f"autoload:{key}" if section == "autoload" else key,
        })

    return {"nodes": nodes, "edges": edges}
