"""GDScript extractor (tree-sitter). Godot .gd scripts."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graphify.extractors.base import _file_stem, _make_id, _read_text
from graphify.extractors.godot_paths import resolve_res_path

# `preload` is resolved at parse time and `load` at run time; both name a file the
# script depends on, which is the only distinction the graph cares about here.
_LOADERS = frozenset({"preload", "load", "ResourceLoader.load"})

# A GDScript call whose name is one of these belongs to the language or to the
# engine's own types, not to the corpus: every script calls them, so resolving
# them by bare name would build god nodes (same reasoning as the shared
# _LANGUAGE_BUILTIN_GLOBALS). The cost is the reverse case — a project that
# defines `func size()` and calls it gets no edge from that call site, though the
# definition is still a node. That trade is the one the shared list already makes.
_GDSCRIPT_BUILTINS = frozenset({
    # @GlobalScope functions
    "print", "printerr", "print_debug", "print_rich", "push_error", "push_warning",
    "str", "int", "float", "bool", "len", "range", "min", "max", "abs", "clamp",
    "minf", "maxf", "absf", "clampf", "mini", "maxi", "absi", "clampi",
    "sign", "signf", "signi", "round", "roundf", "roundi", "floor", "floorf",
    "floori", "ceil", "ceilf", "ceili", "pow", "sqrt", "sin", "cos", "tan",
    "sinh", "cosh", "tanh", "atan", "atan2", "asin", "acos", "exp", "log",
    "fmod", "fposmod", "posmod", "snapped", "snappedf", "snappedi",
    "lerp", "lerpf", "lerp_angle", "inverse_lerp", "remap", "smoothstep",
    "move_toward", "rotate_toward", "wrapf", "wrapi", "ease", "pingpong",
    "cubic_interpolate", "bezier_interpolate", "step_decimals", "nearest_po2",
    "deg_to_rad", "rad_to_deg", "is_nan", "is_inf", "is_finite",
    "is_equal_approx", "is_zero_approx", "is_same",
    "randf", "randi", "randfn", "randf_range", "randi_range", "randomize", "seed",
    "typeof", "type_string", "is_instance_valid", "is_instance_of",
    "instance_from_id", "weakref", "assert", "hash",
    # Built-in type constructors
    "Vector2", "Vector2i", "Vector3", "Vector3i", "Vector4", "Vector4i",
    "Color", "Rect2", "Rect2i", "Transform2D", "Transform3D", "Basis",
    "Quaternion", "Plane", "AABB", "Callable", "Signal", "StringName",
    "NodePath", "PackedByteArray", "PackedInt32Array", "PackedInt64Array",
    "PackedFloat32Array", "PackedFloat64Array", "PackedStringArray",
    "PackedVector2Array", "PackedVector3Array", "PackedColorArray",
    # Object lifetime and tree access, called from nearly every script
    "new", "instantiate", "duplicate", "free", "queue_free", "call_deferred",
    "get_tree", "get_node", "get_node_or_null", "add_child", "remove_child",
    "connect", "disconnect", "emit", "notify_property_list_changed",
    # Methods of the built-in containers and math types
    "size", "append", "append_array", "resize", "clear", "has", "is_empty",
    "erase", "insert", "pop_back", "pop_front", "push_back", "front", "back",
    "keys", "values", "get", "set", "length", "length_squared", "normalized",
    "dot", "cross", "distance_to", "distance_squared_to", "to_byte_array",
})


def _string_literal(node, source: bytes) -> str | None:
    """Text of a GDScript string node with its quotes removed."""
    if node is None or node.type != "string":
        return None
    raw = _read_text(node, source)
    for quote in ('"""', "'''", '"', "'"):
        if raw.startswith(quote) and raw.endswith(quote) and len(raw) >= 2 * len(quote):
            return raw[len(quote):-len(quote)]
    return raw


def _call_callee(node, source: bytes) -> str | None:
    """Name of the function a ``call``/``attribute_call`` node invokes.

    GDScript's grammar gives the callee no field: it is the first identifier
    child, with the argument list following it.
    """
    for child in node.children:
        if child.type == "identifier":
            return _read_text(child, source)
    return None


def _first_string_argument(node, source: bytes) -> str | None:
    args = node.child_by_field_name("arguments")
    if args is None:
        return None
    for child in args.children:
        if child.type == "string":
            return _string_literal(child, source)
    return None


def extract_gdscript(path: Path) -> dict:
    """Extract classes, functions, signals, extends, preloads, and calls from a .gd file."""
    try:
        import tree_sitter_gdscript as tsgd
        from tree_sitter import Language, Parser
    except ImportError:
        return {"nodes": [], "edges": [], "error": "tree_sitter_gdscript not installed"}

    try:
        language = Language(tsgd.language())
        parser = Parser(language)
        source = path.read_bytes()
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = _file_stem(path)
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    node_by_id: dict[str, dict] = {}
    function_bodies: list[tuple[str, Any]] = []
    signal_ids: dict[str, str] = {}

    def add_node(nid: str, label: str, line: int | None, declared: bool = True) -> None:
        """Add a node; a later declaration fills in a placeholder's location.

        ``declared`` is False for a name this file only refers to (a base class
        from the engine, a global class declared in another file). Those carry no
        source_file, so whichever file really declares the name can claim it.
        """
        existing = node_by_id.get(nid)
        if existing is None:
            entry = {
                "id": nid, "label": label, "file_type": "code",
                "source_file": str_path if declared else "",
                "source_location": f"L{line}" if (declared and line) else "",
            }
            nodes.append(entry)
            node_by_id[nid] = entry
            return
        if declared and not existing.get("source_file"):
            existing["source_file"] = str_path
            existing["source_location"] = f"L{line}" if line else ""

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", weight: float = 1.0,
                 context: str | None = None) -> None:
        edge = {"source": src, "target": tgt, "relation": relation,
                "confidence": confidence, "source_file": str_path,
                "source_location": f"L{line}", "weight": weight}
        if context:
            edge["context"] = context
        edges.append(edge)

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)

    def _import_res(raw: str, line: int, context: str) -> str | None:
        """Emit an imports_from edge for a res:// literal; return the target id.

        The target is materialized as a node. Most of what a Godot script loads is
        a file no extractor reads — a shader, a scene, a texture — and without a
        node of its own the edge is a dangling reference build_from_json prunes
        (#1327), which is how a script's whole dependency on its shaders
        disappears. The node carries the target's own path, so when the target IS
        extracted (one .gd preloading another) both sides produce the same id and
        the two descriptions are one node.
        """
        target = resolve_res_path(raw, path)
        if target is None:
            return None
        tgt_nid = _make_id(str(target))
        if tgt_nid not in node_by_id:
            entry = {"id": tgt_nid, "label": target.name, "file_type": "code",
                     "source_file": str(target), "source_location": ""}
            nodes.append(entry)
            node_by_id[tgt_nid] = entry
        add_edge(file_nid, tgt_nid, "imports_from", line, context=context)
        return tgt_nid

    # ── `class_name X` makes X a project-global name in Godot, so its node id is
    # global too: `extends X` in any other file lands on this exact id with no
    # cross-file resolution pass to run.
    class_nid: str | None = None
    class_line = 1
    for child in root.children:
        if child.type == "class_name_statement":
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                class_name = _read_text(name_node, source)
                class_line = child.start_point[0] + 1
                class_nid = _make_id(class_name)
                add_node(class_nid, class_name, class_line)
                add_edge(file_nid, class_nid, "contains", class_line)
            break

    # The type a script extends belongs to the class it declares; a script with no
    # class_name is its own anonymous class, and the file stands in for it.
    owner_nid = class_nid or file_nid

    def _extends(node, subject_nid: str) -> None:
        """Emit inherits for an extends_statement (`extends Node3D` or a res:// path)."""
        line = node.start_point[0] + 1
        for child in node.children:
            if child.type == "type":
                base_name = _read_text(child, source)
                base_nid = _make_id(base_name)
                add_node(base_nid, base_name, None, declared=False)
                add_edge(subject_nid, base_nid, "inherits", line)
                return
            if child.type == "string":
                raw = _string_literal(child, source)
                if raw:
                    tgt_nid = _import_res(raw, line, "extends")
                    if tgt_nid:
                        add_edge(subject_nid, tgt_nid, "inherits", line)
                return

    def walk(node, owner: str, in_class: bool) -> None:
        t = node.type

        if t == "extends_statement":
            _extends(node, owner)
            return

        if t == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return
            inner_name = _read_text(name_node, source)
            line = node.start_point[0] + 1
            # An inner class is scoped to its file, unlike a class_name.
            inner_nid = _make_id(stem, inner_name)
            add_node(inner_nid, inner_name, line)
            add_edge(owner, inner_nid, "contains", line)
            extends_node = node.child_by_field_name("extends")
            if extends_node is not None:
                _extends(extends_node, inner_nid)
            body = node.child_by_field_name("body")
            if body is not None:
                for child in body.children:
                    walk(child, inner_nid, True)
            return

        if t == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                func_name = _read_text(name_node, source)
                line = node.start_point[0] + 1
                if in_class:
                    func_nid = _make_id(owner, func_name)
                    add_node(func_nid, f".{func_name}()", line)
                    add_edge(owner, func_nid, "method", line)
                else:
                    func_nid = _make_id(stem, func_name)
                    add_node(func_nid, f"{func_name}()", line)
                    add_edge(owner, func_nid, "contains", line)
                body = node.child_by_field_name("body")
                if body is not None:
                    function_bodies.append((func_nid, body))
            return

        if t == "signal_statement":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                signal_name = _read_text(name_node, source)
                line = node.start_point[0] + 1
                signal_nid = _make_id(stem, "signal", signal_name)
                add_node(signal_nid, f"{signal_name} (signal)", line)
                add_edge(owner, signal_nid, "contains", line)
                signal_ids[signal_name] = signal_nid
            return

        if t in ("const_statement", "variable_statement"):
            # A const or var holding preload("res://…") — or the resource path
            # itself, which is how a script names a shader it never preloads — is
            # a dependency on that file, and in a Godot project it is usually the
            # only thing tying a script to its shader or scene.
            value = node.child_by_field_name("value")
            line = node.start_point[0] + 1
            if value is not None:
                raw = _string_literal(value, source)
                if raw:
                    _import_res(raw, line, "resource path")
                elif value.type == "call":
                    callee = _call_callee(value, source)
                    if callee in _LOADERS:
                        arg = _first_string_argument(value, source)
                        if arg:
                            _import_res(arg, line, callee)
            return

        for child in node.children:
            walk(child, owner, in_class)

    for child in root.children:
        walk(child, owner_nid, False)

    seen_call_pairs: set[tuple[str, str]] = set()
    raw_calls: list[dict] = []

    def walk_calls(node, caller_nid: str) -> None:
        # A nested function body belongs to its own caller, and GDScript has no
        # nested `func`, so only a class_definition can restart the scope.
        if node.type == "class_definition":
            return

        if node.type in ("call", "attribute_call"):
            callee = _call_callee(node, source)
            line = node.start_point[0] + 1
            if callee in _LOADERS:
                arg = _first_string_argument(node, source)
                if arg:
                    _import_res(arg, line, callee)
            elif callee == "emit_signal":
                arg = _first_string_argument(node, source)
                target = signal_ids.get(arg or "")
                if target:
                    add_edge(caller_nid, target, "references", line, context="emit_signal")
            elif callee and callee not in _GDSCRIPT_BUILTINS:
                is_member_call = node.type == "attribute_call"
                tgt_nid = next(
                    (n["id"] for n in nodes
                     if n["label"] in (f"{callee}()", f".{callee}()")
                     and n.get("source_file") == str_path),
                    None,
                )
                if tgt_nid and tgt_nid != caller_nid:
                    pair = (caller_nid, tgt_nid)
                    if pair not in seen_call_pairs:
                        seen_call_pairs.add(pair)
                        add_edge(caller_nid, tgt_nid, "calls", line)
                else:
                    raw_calls.append({
                        "caller_nid": caller_nid,
                        "callee": callee,
                        "is_member_call": is_member_call,
                        "source_file": str_path,
                        "source_location": f"L{line}",
                    })

        for child in node.children:
            walk_calls(child, caller_nid)

    for caller_nid, body_node in function_bodies:
        walk_calls(body_node, caller_nid)

    seen_ids = set(node_by_id)
    clean_edges = [e for e in edges
                   if e["source"] in seen_ids
                   and (e["target"] in seen_ids or e["relation"] == "imports_from")]
    return {"nodes": nodes, "edges": clean_edges, "raw_calls": raw_calls}
