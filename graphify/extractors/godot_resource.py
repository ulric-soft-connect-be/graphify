"""Godot scene and resource extractor (.tscn, .tres).

Both are the same INI-shaped text format. What ties them into a project's graph is
the ``[ext_resource]`` header: it is how a scene names the script that drives it,
the preset resource it carries, and the sub-scene it instances. Without it a Godot
project's scripts look unconnected to the scenes that run them.
"""
from __future__ import annotations

import re

from pathlib import Path

from graphify.extractors.base import _make_id
from graphify.extractors.godot_paths import resolve_res_path

_EXT_RESOURCE_RE = re.compile(r'^\[ext_resource\s+([^\]]*)\]', re.M)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


def extract_godot_resource(path: Path) -> dict:
    """Extract ext_resource dependencies from a .tscn / .tres file."""
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

    for m in _EXT_RESOURCE_RE.finditer(src):
        attrs = dict(_ATTR_RE.findall(m.group(1)))
        raw = attrs.get("path", "")
        target = resolve_res_path(raw, path)
        if target is None:
            continue
        tgt_nid = _make_id(str(target))
        line = src.count("\n", 0, m.start()) + 1
        add_node(tgt_nid, target.name, str(target), None)
        if tgt_nid in seen_targets:
            # A scene naming the same script twice is one dependency, not two.
            continue
        seen_targets.add(tgt_nid)
        edges.append({
            "source": file_nid, "target": tgt_nid, "relation": "imports_from",
            "confidence": "EXTRACTED", "source_file": str_path,
            "source_location": f"L{line}", "weight": 1.0,
            "context": (attrs.get("type") or "ext_resource").lower(),
        })

    return {"nodes": nodes, "edges": edges}
