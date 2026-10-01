"""Cross-file resolution for GDScript: typed member calls, type references, signals.

``extract_gdscript`` sees one file at a time, so it resolves only calls into its own
functions. Everything a Godot script does with another file's code goes through a
``class_name`` (a project-global name), an autoload (a global singleton named in
``project.godot``) or a variable typed as one of those, and none of that leaves an
edge on its own:

- ``_board.bind(state)`` where ``@onready var _board: BoardView = $Board``;
- ``MineField.chebyshev(offset)``, ``FieldState.new(field)``, ``FieldState.Cell.HIDDEN``;
- ``var x: RunStats``, ``func f(state: FieldState) -> BoardView``, ``node as ChunkView``;
- ``EventBus.run_started.emit(seed)``, ``_input.primary_pressed.connect(act_primary)``.

Measured on one project (mine-horizon, 2026-10-01): the controller that creates and
queries the pure layer on every move had no edge to any of it, and about twenty
methods called only from other files looked like dead code.

GDScript is dynamically typed, so this pass resolves only what the source states.
A receiver's type comes from a type annotation, ``X.new()``, ``expr as X``, a
``preload("res://x.gd")`` constant, an autoload name, or the declared return type
of a project method. An untyped receiver yields no edge, which rules out a
fabricated one. Every edge is EXTRACTED, the convention of the shared member-call
resolvers for a receiver whose type the source names (#1533).

Edges emitted (all additive, all sourced by the file being extracted):

=========================  ==============================  =====================
from                       to                              relation / context
=========================  ==============================  =====================
calling function           method of the receiver's class  calls / call
calling function           class (``X.new()``)             references / new
calling function           class (``X.CONST``, ``X.Enum``) references / member
function, class or signal  class named in a type           references / type
connecting function        signal                          references / connect
signal                     connected handler               calls / signal:<name>
emitting function          signal                          references / emit
subclass                   project base class              inherits
=========================  ==============================  =====================

A signal wired in code is the same fact as a ``[connection]`` in a scene, which
``godot_resource`` turns into a ``calls`` edge carrying ``signal:<name>``. Here the
signal is a node of its own, so the edge leaves the signal rather than the file.

**Incremental rebuilds.** The resolver registry hands this pass the unchanged
corpus's nodes, but only a fixed list of metadata keys travels with them (watch.py,
cli.py), and a script's member types are not among them. This pass therefore
re-reads an unchanged script from disk to learn its declarations. Edges still come
only from the files being extracted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from graphify.extractors.base import _read_text
from graphify.extractors.godot_paths import godot_root, resolve_res_path

# Wrappers around a callable that still name the handler: `_refresh.bind(3)`.
_CALLABLE_WRAPPERS = frozenset({"bind", "bindv", "unbind"})
_LOADERS = frozenset({"preload", "load"})
_AUTOLOAD_RE = re.compile(r'^([A-Za-z_]\w*)\s*=\s*"\*?(res://[^"]+)"\s*$')

# A value type: ("instance", script) | ("class", script) | ("signal", script, name).
_Type = tuple


@dataclass
class _Function:
    name: str
    node: Any
    returns: str | None
    # name -> type spec: ("type", "Name") | ("expr", node) | ("script", Path)
    variables: dict = field(default_factory=dict)


@dataclass
class _Script:
    path: Path
    source: bytes
    root: Any
    class_name: str | None = None
    base: str | None = None
    base_path: Path | None = None
    members: dict = field(default_factory=dict)
    statics: set = field(default_factory=set)
    signals: set = field(default_factory=set)
    functions: dict = field(default_factory=dict)
    # graph node ids, by label, for this file
    ids: dict = field(default_factory=dict)
    fresh: bool = False
    source_file: str = ""

    def owner_label(self) -> str:
        return self.class_name or self.path.name


def _parser():
    import tree_sitter_gdscript as tsgd
    from tree_sitter import Language, Parser
    return Parser(Language(tsgd.language()))


def _text(node, script: _Script) -> str:
    return _read_text(node, script.source)


def _type_spec(type_node, script: _Script):
    """("type", name) for a plain type annotation; None for a container or none."""
    if type_node is None or type_node.type != "type":
        return None
    named = [c for c in type_node.children if c.type == "identifier"]
    if len(named) == 1 and len(type_node.children) == 1:
        return ("type", _text(named[0], script))
    return None


def _value_spec(value, script: _Script):
    if value is None:
        return None
    if value.type == "call":
        callee = next((c for c in value.children if c.type == "identifier"), None)
        if callee is not None and _text(callee, script) in _LOADERS:
            args = value.child_by_field_name("arguments")
            raw = next((c for c in (args.children if args else []) if c.type == "string"), None)
            if raw is not None:
                literal = _text(raw, script).strip("\"'")
                target = resolve_res_path(literal, script.path)
                if target is not None and target.suffix == ".gd":
                    return ("script", target)
    return ("expr", value)


def _declaration_spec(node, script: _Script):
    """Type spec of a var/const statement: its annotation, else its value."""
    spec = _type_spec(node.child_by_field_name("type"), script)
    if spec is not None:
        return spec
    return _value_spec(node.child_by_field_name("value"), script)


def _collect_variables(node, script: _Script, into: dict) -> None:
    """Parameters and locals of a function, lambdas included, by name."""
    t = node.type
    if t == "typed_parameter":
        name = next((c for c in node.children if c.type == "identifier"), None)
        spec = _type_spec(node.child_by_field_name("type"), script)
        if name is not None and spec is not None:
            into.setdefault(_text(name, script), spec)
        return
    if t in ("typed_default_parameter", "default_parameter"):
        name = next((c for c in node.children if c.type == "identifier"), None)
        spec = _type_spec(node.child_by_field_name("type"), script)
        if spec is None:
            spec = _value_spec(node.child_by_field_name("value"), script)
        if name is not None and spec is not None:
            into.setdefault(_text(name, script), spec)
        return
    if t == "variable_statement":
        name = node.child_by_field_name("name")
        spec = _declaration_spec(node, script)
        if name is not None and spec is not None:
            into.setdefault(_text(name, script), spec)
    elif t == "for_statement":
        left = node.child_by_field_name("left")
        spec = _type_spec(node.child_by_field_name("type"), script)
        if left is not None and spec is not None:
            into.setdefault(_text(left, script), spec)
    for child in node.children:
        _collect_variables(child, script, into)


def _parse_script(path: Path, parser) -> _Script | None:
    try:
        source = path.read_bytes()
        tree = parser.parse(source)
    except Exception:
        return None
    script = _Script(path=path, source=source, root=tree.root_node)
    for child in tree.root_node.children:
        _declare(child, script)
    return script


def _declare(node, script: _Script) -> None:
    t = node.type
    if t == "class_name_statement":
        name = node.child_by_field_name("name")
        if name is not None:
            script.class_name = _text(name, script)
        for child in node.children:
            if child.type == "extends_statement":
                _declare(child, script)
    elif t == "extends_statement":
        for child in node.children:
            if child.type == "type":
                script.base = _text(child, script)
            elif child.type == "string":
                script.base_path = resolve_res_path(_text(child, script).strip("\"'"), script.path)
    elif t == "variable_statement":
        name = node.child_by_field_name("name")
        if name is not None:
            script.members[_text(name, script)] = _declaration_spec(node, script)
    elif t == "const_statement":
        name = node.child_by_field_name("name")
        if name is not None:
            key = _text(name, script)
            script.statics.add(key)
            script.members[key] = _declaration_spec(node, script)
    elif t in ("enum_definition", "class_definition"):
        name = node.child_by_field_name("name")
        if name is not None:
            script.statics.add(_text(name, script))
    elif t == "signal_statement":
        name = node.child_by_field_name("name")
        if name is not None:
            script.signals.add(_text(name, script))
    elif t == "function_definition":
        name = node.child_by_field_name("name")
        if name is None:
            return
        returns = _type_spec(node.child_by_field_name("return_type"), script)
        function = _Function(_text(name, script), node, returns[1] if returns else None)
        for part in ("parameters", "body"):
            child = node.child_by_field_name(part)
            if child is not None:
                _collect_variables(child, script, function.variables)
        script.functions[function.name] = function


def _reassociate(parts: list) -> list:
    """Undo tree-sitter-gdscript's split of a chain that follows an operator.

    The grammar reads ``a and s.f.g(c)`` as ``(a and s.f).g(c)``: an attribute
    whose first part is the operator, holding the start of the chain as its right
    operand. It does the same after ``==``, ``or``, ``+``, ``not``, unary ``-``.
    A chain cannot really start with a bare operator expression, since GDScript
    would need parentheses (``(a + b).f()`` is a ``parenthesized_expression``).
    So the chain is the operand's chain followed by the remaining segments. The
    left operand holds chains of its own, and the body walk visits those
    separately.
    """
    while parts and parts[0].type in ("binary_operator", "unary_operator"):
        head = parts[0]
        operand = head.child_by_field_name("right")
        if operand is None:
            named = [c for c in head.children if c.is_named]
            operand = named[-1] if named else None
        if operand is None:
            return []
        if operand.type == "attribute":
            parts = [c for c in operand.children if c.type != "."] + parts[1:]
        else:
            parts = [operand] + parts[1:]
    return parts


def _path_key(source_file: str) -> tuple[str, ...]:
    return tuple(Path(source_file).as_posix().split("/"))


def _same_file(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Whether two path spellings name one file: equal, or one a tail of the other.

    At resolution time a freshly extracted file carries the path it was handed
    (often absolute) while an unchanged one, read back from graph.json, carries
    its scan-root-relative path.
    """
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return bool(short) and long[-len(short):] == short


def _locate(source_file: str, roots: list[Path]) -> Path | None:
    """Find on disk a context script known only by a relative path."""
    path = Path(source_file)
    if path.is_absolute():
        return path if path.is_file() else None
    candidates = [Path.cwd() / path]
    for root in roots:
        probe = root
        for _ in range(6):
            candidates.append(probe / path)
            if probe.parent == probe:
                break
            probe = probe.parent
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


class _Resolver:
    def __init__(self, per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]):
        self.all_nodes = all_nodes
        self.all_edges = all_edges
        self.node_by_id = {n.get("id"): n for n in all_nodes}
        self.node_ids = set(self.node_by_id)
        self.memo: dict = {}
        self.existing = {(e.get("source"), e.get("target"), e.get("relation"))
                         for e in all_edges}
        self.parser = _parser()
        self.scripts: list[_Script] = []
        self.by_class: dict[str, _Script] = {}
        self.by_path: dict[Path, _Script] = {}
        self.autoloads: dict[str, _Script] = {}
        self._load(per_file)

    # ── corpus ─────────────────────────────────────────────────────────────
    def _load(self, per_file: list[dict]) -> None:
        fresh: dict[str, str] = {}
        for result in per_file:
            for node in result.get("nodes", []) or []:
                source_file = str(node.get("source_file") or "")
                if source_file.endswith(".gd") and node.get("label") == Path(source_file).name:
                    fresh[source_file] = source_file
                    break
        roots: list[Path] = []
        for source_file in fresh:
            root = godot_root(Path(source_file).resolve())
            if root is not None and root not in roots:
                roots.append(root)

        nodes_by_name: dict[str, list[dict]] = {}
        for node in self.all_nodes:
            source_file = str(node.get("source_file") or "")
            if source_file.endswith(".gd"):
                nodes_by_name.setdefault(Path(source_file).name, []).append(node)

        known: list[tuple[str, bool]] = [(sf, True) for sf in fresh]
        seen_keys = [_path_key(sf) for sf in fresh]
        for node in self.all_nodes:
            source_file = str(node.get("source_file") or "")
            if not source_file.endswith(".gd"):
                continue
            key = _path_key(source_file)
            if any(_same_file(key, k) for k in seen_keys):
                continue
            seen_keys.append(key)
            known.append((source_file, False))

        for source_file, is_fresh in known:
            path = Path(source_file).resolve() if is_fresh else _locate(source_file, roots)
            if path is None:
                continue
            script = _parse_script(path, self.parser)
            if script is None:
                continue
            script.fresh = is_fresh
            script.source_file = source_file
            key = _path_key(source_file)
            for node in nodes_by_name.get(path.name, []):
                if _same_file(_path_key(str(node.get("source_file"))), key):
                    script.ids.setdefault(str(node.get("label")), node.get("id"))
            self.scripts.append(script)
            self.by_path[path] = script
            root = godot_root(path)
            if root is not None and root not in roots:
                roots.append(root)

        declared: dict[str, list[_Script]] = {}
        for script in self.scripts:
            if script.class_name:
                declared.setdefault(script.class_name, []).append(script)
        # Two files declaring one class_name is a project error Godot refuses;
        # resolving to either would be a guess.
        self.by_class = {name: s[0] for name, s in declared.items() if len(s) == 1}

        for root in roots:
            self._read_autoloads(root)

    def _read_autoloads(self, root: Path) -> None:
        try:
            lines = (root / "project.godot").read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        section = ""
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1]
                continue
            if section != "autoload":
                continue
            match = _AUTOLOAD_RE.match(stripped)
            if match is None:
                continue
            target = resolve_res_path(match.group(2), root / "project.godot")
            script = self.by_path.get(target.resolve()) if target is not None else None
            if script is not None:
                self.autoloads.setdefault(match.group(1), script)

    # ── lookups ─────────────────────────────────────────────────────────────
    def _base_of(self, script: _Script) -> _Script | None:
        if script.base_path is not None:
            return self.by_path.get(script.base_path.resolve())
        if script.base:
            return self.by_class.get(script.base)
        return None

    def _lineage(self, script: _Script):
        seen: set[int] = set()
        while script is not None and id(script) not in seen:
            seen.add(id(script))
            yield script
            script = self._base_of(script)

    def _function(self, script: _Script, name: str):
        for owner in self._lineage(script):
            if name in owner.functions:
                return owner, owner.functions[name]
        return None

    def _member(self, script: _Script, name: str):
        for owner in self._lineage(script):
            if name in owner.members:
                return owner, owner.members[name]
        return None

    def _signal(self, script: _Script, name: str):
        for owner in self._lineage(script):
            if name in owner.signals:
                return owner
        return None

    def _static(self, script: _Script, name: str):
        for owner in self._lineage(script):
            if name in owner.statics:
                return owner
        return None

    def _owner_id(self, script: _Script) -> str | None:
        return script.ids.get(script.owner_label())

    def _function_id(self, script: _Script, name: str) -> str | None:
        return script.ids.get(f"{name}()")

    def _signal_id(self, script: _Script, name: str) -> str | None:
        return script.ids.get(f"{name} (signal)")

    # ── typing ──────────────────────────────────────────────────────────────
    def _spec_type(self, spec, script: _Script, function: _Function | None, guard: set):
        if spec is None:
            return None
        kind, value = spec
        if kind == "type":
            target = self.by_class.get(value)
            return ("instance", target) if target is not None else None
        if kind == "script":
            target = self.by_path.get(Path(value).resolve())
            return ("class", target) if target is not None else None
        # A declaration's type is computed once and remembered. `guard` holds only
        # the declarations being computed right now, so `var a := b` / `var b := a`
        # stops instead of recursing, and a variable used twice is typed twice.
        key = (id(script), value.start_byte, value.end_byte)
        if key in self.memo:
            return self.memo[key]
        if key in guard:
            return None
        guard.add(key)
        try:
            result = self._type_of(value, script, function, None, guard)
        finally:
            guard.discard(key)
        self.memo[key] = result
        return result

    def _name_type(self, name: str, script: _Script, function: _Function | None, guard: set):
        if name == "self":
            return ("instance", script)
        if function is not None and name in function.variables:
            return self._spec_type(function.variables[name], script, function, guard)
        member = self._member(script, name)
        if member is not None:
            owner, spec = member
            return self._spec_type(spec, owner, None, guard)
        signal_owner = self._signal(script, name)
        if signal_owner is not None:
            return ("signal", signal_owner, name)
        if name in self.by_class:
            return ("class", self.by_class[name])
        if name in self.autoloads:
            return ("instance", self.autoloads[name])
        return None

    def _type_of(self, node, script: _Script, function: _Function | None,
                 caller: str | None, guard: set):
        """Type of an expression; with ``caller`` set, also emit the edges it implies."""
        t = node.type
        if t == "identifier":
            return self._name_type(_text(node, script), script, function, guard)
        if t == "attribute":
            return self._chain(node, script, function, caller, guard)
        if t == "call":
            callee = next((c for c in node.children if c.type == "identifier"), None)
            if callee is None:
                return None
            found = self._function(script, _text(callee, script))
            if found is None:
                return None
            owner, target = found
            if caller is not None and owner is not script:
                self._emit(caller, self._function_id(owner, target.name), "calls", node, script, "call")
            return self._spec_type(("type", target.returns) if target.returns else None, owner, None, guard)
        if t == "binary_operator":
            op = node.child_by_field_name("op")
            right = node.child_by_field_name("right")
            if op is not None and right is not None and _text(op, script) == "as":
                target = self.by_class.get(_text(right, script))
                return ("instance", target) if target is not None else None
            return None
        if t in ("parenthesized_expression", "await_expression"):
            inner = [c for c in node.children if c.is_named]
            return self._type_of(inner[0], script, function, caller, guard) if inner else None
        return None

    def _chain(self, node, script: _Script, function: _Function | None,
               caller: str | None, guard: set):
        parts = _reassociate([c for c in node.children if c.type != "."])
        if not parts:
            return None
        current = self._type_of(parts[0], script, function, caller, guard)
        for segment in parts[1:]:
            if current is None:
                return None
            if segment.type == "identifier":
                current = self._field(current, _text(segment, script), segment, script, caller, guard)
            elif segment.type == "attribute_call":
                current = self._member_call(current, segment, script, function, caller, guard)
            else:
                return None
        return current

    def _field(self, current: _Type, name: str, node, script: _Script, caller, guard):
        kind, target = current[0], current[1]
        if kind not in ("instance", "class"):
            return None
        signal_owner = self._signal(target, name)
        if signal_owner is not None:
            return ("signal", signal_owner, name)
        static_owner = self._static(target, name)
        if static_owner is not None and caller is not None and static_owner is not script:
            self._emit(caller, self._owner_id(static_owner), "references", node, script, "member")
        member = self._member(target, name)
        if member is not None:
            owner, spec = member
            return self._spec_type(spec, owner, None, guard)
        return None

    def _member_call(self, current: _Type, segment, script: _Script, function, caller, guard):
        callee = next((c for c in segment.children if c.type == "identifier"), None)
        if callee is None:
            return None
        method = _text(callee, script)
        kind = current[0]
        if kind == "signal":
            if caller is not None:
                owner, name = current[1], current[2]
                signal_id = self._signal_id(owner, name)
                if method == "connect":
                    self._emit(caller, signal_id, "references", segment, script, "connect")
                    args = segment.child_by_field_name("arguments")
                    handler = next((c for c in (args.children if args else []) if c.is_named), None)
                    if handler is not None:
                        self._emit(signal_id, self._handler_id(handler, script, function, guard),
                                   "calls", segment, script, f"signal:{name}")
                elif method == "emit":
                    self._emit(caller, signal_id, "references", segment, script, "emit")
            return None
        target = current[1]
        if method == "new" and kind == "class":
            if caller is not None and target is not script:
                self._emit(caller, self._owner_id(target), "references", segment, script, "new")
            return ("instance", target)
        found = self._function(target, method)
        if found is None:
            return None
        owner, resolved = found
        if caller is not None:
            self._emit(caller, self._function_id(owner, resolved.name), "calls", segment, script, "call")
        returns = ("type", resolved.returns) if resolved.returns else None
        return self._spec_type(returns, owner, None, guard)

    def _handler_id(self, node, script: _Script, function, guard) -> str | None:
        """The function a connected handler expression names, or None (a lambda...)."""
        if node.type == "identifier":
            found = self._function(script, _text(node, script))
            return self._function_id(found[0], found[1].name) if found else None
        if node.type != "attribute":
            return None
        parts = [c for c in node.children if c.type != "."]
        if parts and parts[-1].type == "attribute_call":
            wrapper = next((c for c in parts[-1].children if c.type == "identifier"), None)
            if wrapper is None or _text(wrapper, script) not in _CALLABLE_WRAPPERS:
                return None
            parts = parts[:-1]
        if not parts or parts[-1].type != "identifier":
            return None
        name = _text(parts[-1], script)
        if len(parts) == 1:
            found = self._function(script, name)
        else:
            receiver = self._type_of(parts[0], script, function, None, guard)
            for segment in parts[1:-1]:
                if receiver is None or segment.type != "identifier":
                    receiver = None
                    break
                receiver = self._field(receiver, _text(segment, script), segment, script, None, guard)
            if receiver is None or receiver[0] not in ("instance", "class"):
                return None
            found = self._function(receiver[1], name)
        return self._function_id(found[0], found[1].name) if found else None

    # ── emission ────────────────────────────────────────────────────────────
    def _emit(self, source: str | None, target: str | None, relation: str, node,
              script: _Script, context: str) -> None:
        """Append one edge; ``node`` is the syntax node it comes from, or a line number."""
        if not source or not target or source == target:
            return
        if source not in self.node_ids or target not in self.node_ids:
            return
        key = (source, target, relation)
        if key in self.existing:
            return
        self.existing.add(key)
        self.all_edges.append({
            "source": source, "target": target, "relation": relation,
            "confidence": "EXTRACTED", "confidence_score": 1.0,
            "source_file": script.source_file,
            "source_location": f"L{node if isinstance(node, int) else node.start_point[0] + 1}",
            "weight": 1.0, "context": context,
        })

    def _type_references(self, node, script: _Script, source: str | None) -> None:
        """Emit `references / type` for every project class a type node names."""
        if node.type == "type":
            for name in self._identifiers(node, script):
                target = self.by_class.get(name)
                if target is not None and target is not script:
                    self._emit(source, self._owner_id(target), "references", node, script, "type")
            return
        if node.type == "binary_operator":
            op = node.child_by_field_name("op")
            right = node.child_by_field_name("right")
            if op is not None and right is not None and _text(op, script) in ("as", "is"):
                target = self.by_class.get(_text(right, script))
                if target is not None and target is not script:
                    self._emit(source, self._owner_id(target), "references", right, script, "type")
        for child in node.children:
            self._type_references(child, script, source)

    def _identifiers(self, node, script: _Script):
        if node.type == "identifier":
            yield _text(node, script)
        for child in node.children:
            yield from self._identifiers(child, script)

    def _walk_body(self, node, script: _Script, function: _Function | None,
                   caller: str, guard: set) -> None:
        t = node.type
        if t == "attribute":
            self._chain(node, script, function, caller, guard)
            # Arguments hold chains of their own: `a.b(c.d())`.
            for part in node.children:
                if part.type == "attribute_call":
                    args = part.child_by_field_name("arguments")
                    if args is not None:
                        self._walk_body(args, script, function, caller, guard)
                elif part.type not in ("identifier", "."):
                    self._walk_body(part, script, function, caller, guard)
            return
        if t == "call":
            self._type_of(node, script, function, caller, guard)
        for child in node.children:
            self._walk_body(child, script, function, caller, guard)

    def _inherits(self, script: _Script) -> None:
        """`extends Base` by name, for the script's class and its inner classes.

        The extractor recorded the name on each subclass node
        (``metadata.godot_extends``). A name no project file declares is an engine
        class and gets nothing, which is the point: no phantom ``Node3D`` node.
        """
        for node_id in set(script.ids.values()):
            node = self.node_by_id.get(node_id) or {}
            meta = node.get("metadata")
            base_name = meta.get("godot_extends") if isinstance(meta, dict) else None
            base = self.by_class.get(base_name) if base_name else None
            if base is None:
                continue
            line = str(node.get("source_location") or "L1").lstrip("L")
            self._emit(node_id, self._owner_id(base), "inherits", int(line) if line.isdigit() else 1,
                       script, "extends")

    # ── driver ──────────────────────────────────────────────────────────────
    def run(self) -> None:
        for script in self.scripts:
            if not script.fresh:
                continue
            owner_id = self._owner_id(script)
            self._inherits(script)
            for child in script.root.children:
                t = child.type
                if t == "function_definition":
                    name = child.child_by_field_name("name")
                    function = script.functions.get(_text(name, script)) if name is not None else None
                    if function is None:
                        continue
                    caller = self._function_id(script, function.name)
                    for part in ("parameters", "return_type", "body"):
                        sub = child.child_by_field_name(part)
                        if sub is not None:
                            self._type_references(sub, script, caller)
                    body = child.child_by_field_name("body")
                    if body is not None:
                        self._walk_body(body, script, function, caller, set())
                elif t in ("variable_statement", "const_statement"):
                    self._type_references(child, script, owner_id)
                    value = child.child_by_field_name("value")
                    if value is not None:
                        self._walk_body(value, script, None, owner_id, set())
                elif t == "signal_statement":
                    name = child.child_by_field_name("name")
                    params = child.child_by_field_name("parameters")
                    if name is not None and params is not None:
                        self._type_references(params, script, self._signal_id(script, _text(name, script)))


def resolve_gdscript(per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]) -> None:
    """Resolver-registry entry point (``graphify.resolver_registry``), for ``.gd``."""
    try:
        _Resolver(per_file, all_nodes, all_edges).run()
    except ImportError:
        return  # tree-sitter-gdscript absent: extract_gdscript already reported it
