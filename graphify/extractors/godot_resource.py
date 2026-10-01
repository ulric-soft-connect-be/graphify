"""Godot scene and resource extractor (.tscn, .tres).

Both are the same INI-shaped text format, and two of its headers are what tie a
Godot project together. ``[ext_resource]`` is how a scene names the script that
drives it, the preset resource it carries, and the sub-scene it instances.
``[connection]`` is how the editor wires a signal to its handler: the scene file
is the only place that names the handler, so without it the ``_on_*`` half of a
Godot codebase — the method behind every button, timer and area in the game —
reads as dead code with nothing pointing at it.
"""
from __future__ import annotations

import re

from pathlib import Path

from graphify.extractors.base import _file_stem, _make_id
from graphify.extractors.godot_paths import resolve_res_path

# The format's entire section vocabulary. Matching a header against it — rather
# than against any line in brackets — is what keeps a property holding a wrapped
# array (``points = [1, 2,`` / ``3, 4]``) from being read as a section of its own.
_SECTION_KINDS = frozenset({
    "gd_scene", "gd_resource", "ext_resource", "sub_resource",
    "node", "connection", "editable", "resource",
})
_HEADER_RE = re.compile(r'^\[(\w+)\s*([^\]]*)\]\s*$')
# Godot 4 quotes every attribute value; Godot 3 writes bare ids (`id=1`).
_ATTR_RE = re.compile(r'(\w+)=(?:"([^"]*)"|([^\s\]]+))')
# `script = ExtResource("1_abcde")` (Godot 4) or `script = ExtResource( 1 )` (Godot 3).
# A node's script is a property under its header, not an attribute of it.
_SCRIPT_PROP_RE = re.compile(r'^\s*script\s*=\s*ExtResource\(\s*"?([^"\s)]+)"?\s*\)', re.M)
# A top-level `func`: an indented one is a method of an inner class, which no
# scene connection can reach.
_FUNC_RE = re.compile(r'^(?:static\s+)?func\s+(\w+)\s*\(', re.M)


def _attrs(text: str) -> dict[str, str]:
    """Attributes of a section header, quoted (Godot 4) or bare (Godot 3)."""
    return {key: quoted or bare for key, quoted, bare in _ATTR_RE.findall(text)}


def _node_path(attrs: dict[str, str]) -> str:
    """The path a ``[connection]`` uses to name this node.

    Godot writes paths relative to the scene root: the root itself has no
    ``parent`` and is addressed as ``.``, its children as ``Name``, and deeper
    nodes as ``Parent/Name``.
    """
    name = attrs.get("name", "")
    parent = attrs.get("parent")
    if parent is None:
        return "."
    if parent == ".":
        return name
    return f"{parent}/{name}"


def extract_godot_resource(path: Path) -> dict:
    """Extract ext_resource dependencies and signal connections from .tscn / .tres."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"nodes": [], "edges": [], "error": f"cannot read {path}"}

    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    seen_targets: set[str] = set()
    func_cache: dict[Path, dict[str, int]] = {}

    def add_node(nid: str, label: str, source_file: str, line: int | None) -> None:
        if nid in seen_ids:
            return
        seen_ids.add(nid)
        nodes.append({"id": nid, "label": label, "file_type": "code",
                      "source_file": source_file,
                      "source_location": f"L{line}" if line else ""})

    def top_level_funcs(script: Path) -> dict[str, int]:
        """Line of every top-level ``func`` in a .gd file, by name.

        Cached per extraction: one scene connects several signals to the same
        script, and re-reading it once per connection is the difference between
        one read and a dozen.
        """
        cached = func_cache.get(script)
        if cached is None:
            try:
                text = script.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            cached = {m.group(1): text.count("\n", 0, m.start()) + 1
                      for m in _FUNC_RE.finditer(text)}
            func_cache[script] = cached
        return cached

    file_nid = _make_id(str_path)
    add_node(file_nid, path.name, str_path, 1)

    # ── Sections, in file order, each with the property lines below its header ──
    sections: list[tuple[str, dict[str, str], int, list[str]]] = []
    current: tuple[str, dict[str, str], int, list[str]] | None = None
    for lineno, line in enumerate(src.splitlines(), 1):
        header = _HEADER_RE.match(line)
        if header is not None and header.group(1) in _SECTION_KINDS:
            current = (header.group(1), _attrs(header.group(2)), lineno, [])
            sections.append(current)
        elif current is not None:
            current[3].append(line)

    # ── [ext_resource]: the files this scene or resource is built from ──
    ext_paths: dict[str, Path] = {}
    for kind, attrs, lineno, _body in sections:
        if kind != "ext_resource":
            continue
        target = resolve_res_path(attrs.get("path", ""), path)
        if target is None:
            continue
        ext_paths[attrs.get("id", "")] = target
        tgt_nid = _make_id(str(target))
        add_node(tgt_nid, target.name, str(target), None)
        if tgt_nid in seen_targets:
            # A scene naming the same script twice is one dependency, not two.
            continue
        seen_targets.add(tgt_nid)
        edges.append({
            "source": file_nid, "target": tgt_nid, "relation": "imports_from",
            "confidence": "EXTRACTED", "source_file": str_path,
            "source_location": f"L{lineno}", "weight": 1.0,
            "context": (attrs.get("type") or "ext_resource").lower(),
            # `enemy.tscn` driven by `enemy.gd` is two files on one id until the
            # collision pass salts them apart; without the resolved file to key on,
            # the edge to the script becomes a self-loop and the edge from any
            # other scene stays on the dead shared id (#1814).
            "target_file": str(target),
        })

    # ── [node]: which script drives each node of the tree ──
    scripts_by_node: dict[str, Path] = {}
    for kind, attrs, _lineno, body in sections:
        if kind != "node":
            continue
        prop = _SCRIPT_PROP_RE.search("\n".join(body))
        if prop is None:
            continue
        script = ext_paths.get(prop.group(1))
        # A built-in script (`SubResource`) or a C# one has no `func` to point at.
        if script is not None and script.suffix == ".gd":
            scripts_by_node[_node_path(attrs)] = script

    # ── [connection]: the handler the editor wired to a signal ──
    seen_handlers: set[tuple[str, str]] = set()
    for kind, attrs, lineno, _body in sections:
        if kind != "connection":
            continue
        method = attrs.get("method", "")
        script = scripts_by_node.get(attrs.get("to", ""))
        if not method or script is None:
            continue
        func_line = top_level_funcs(script).get(method)
        if func_line is None:
            # The handler is inherited from a base script, or it belongs to an
            # instanced sub-scene whose own file declares it. Either way this file
            # cannot say which node id that is, and guessing would attribute a
            # method to a file that does not define it.
            continue
        tgt_nid = _make_id(_file_stem(script), method)
        add_node(tgt_nid, f"{method}()", str(script), func_line)
        signal = attrs.get("signal", "")
        if (tgt_nid, signal) in seen_handlers:
            continue
        seen_handlers.add((tgt_nid, signal))
        edges.append({
            "source": file_nid, "target": tgt_nid, "relation": "calls",
            "confidence": "EXTRACTED", "source_file": str_path,
            "source_location": f"L{lineno}", "weight": 1.0,
            "context": f"signal:{signal}" if signal else "signal",
        })

    return {"nodes": nodes, "edges": edges}
