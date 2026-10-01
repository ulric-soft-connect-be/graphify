"""Cross-file GDScript resolution: typed member calls, type references, signals.

The per-file extractor resolves calls into its own file only. Everything a Godot
script does with another file's code goes through a `class_name`, an autoload or a
variable typed as one of those, and graphify.extractors.gdscript_resolution turns
those into edges. These tests build a small project in which every pattern
appears once, plus the cases that must yield nothing.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from graphify.extract import extract

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("tree_sitter_gdscript") is None,
    reason="tree-sitter-gdscript not installed",
)

_FILES = {
    "project.godot": (
        'config_version=5\n\n[autoload]\n\nBus="*res://bus.gd"\n'
    ),
    "bus.gd": (
        "extends Node\n\n"
        "signal scored(points: int)\n"
        "signal finished(report: Report)\n"
    ),
    "report.gd": (
        "class_name Report\nextends RefCounted\n\n"
        "var total := 0\n\n"
        "func add(n: int) -> bool:\n\ttotal += n\n\treturn true\n"
    ),
    "grid.gd": (
        "class_name Grid\nextends RefCounted\n\n"
        "const SIZE := 8\n"
        "enum Kind { A, B }\n"
        "var rules: Report\n\n"
        "static func cell_of(x: int) -> int:\n\treturn x\n\n"
        "func report() -> Report:\n\treturn rules\n"
    ),
    "special_grid.gd": (
        "class_name SpecialGrid\nextends Grid\n\n"
        "func use(node: Node) -> void:\n"
        "\tvar g := node as Grid\n"
        "\tg.report()\n"
    ),
    "board.gd": (
        "class_name Board\nextends Node3D\n\n"
        "signal picked(cell: int)\n\n"
        "func bind(grid: Grid) -> void:\n\tpass\n\n"
        "func set_hover(cell: int) -> void:\n\tpass\n"
    ),
    "game.gd": (
        "class_name Game\nextends Node\n\n"
        "@onready var _board: Board = $Board\n"
        "var _grid: Grid\n\n"
        "func _ready() -> void:\n"
        "\t_board.picked.connect(_on_picked)\n"
        "\t_board.picked.connect(_board.set_hover)\n"
        "\tBus.scored.connect(_on_scored.bind(1))\n"
        "\tBus.finished.connect(func(r: Report) -> void: pass)\n\n"
        "func start() -> void:\n"
        "\t_grid = Grid.new()\n"
        "\t_board.bind(_grid)\n"
        "\tvar n := 1\n"
        # tree-sitter-gdscript reads this as `(n > 0 and _grid.report()).add(1)`.
        "\tif n > 0 and _grid.report().add(1):\n\t\tpass\n"
        "\tBus.scored.emit(3)\n\n"
        "func measure() -> int:\n"
        "\tvar kind := Grid.Kind.A\n"
        "\treturn Grid.cell_of(Grid.SIZE)\n\n"
        "func _on_picked(cell: int) -> void:\n\tpass\n\n"
        "func _on_scored(points: int, extra: int) -> void:\n\tpass\n"
    ),
    "untyped.gd": (
        "extends Node\n\n"
        "var thing\n\n"
        "func poke():\n"
        "\tthing.bind(null)\n"
        "\tvar x = get_node(\"A\")\n"
        "\tx.report()\n"
    ),
    # class_name Hud in ui/hud.gd: its global id `hud` is also what the pre-#1504
    # scheme made of that file's bare stem (the current one is `ui_hud`). At the
    # project root the two schemes agree, and the test would prove nothing.
    "ui/hud.gd": "class_name Hud\nextends CanvasLayer\n",
}


def _write(tmp_path: Path) -> list[Path]:
    paths = []
    for name, text in _FILES.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return paths


@pytest.fixture
def project(tmp_path):
    paths = _write(tmp_path)
    result = extract(paths, cache_root=tmp_path, root=tmp_path, parallel=False)
    return tmp_path, paths, result


def _named(result: dict):
    by_id = {n["id"]: n for n in result["nodes"]}

    def name(nid: str) -> str:
        node = by_id.get(nid, {})
        return f"{Path(str(node.get('source_file', '?'))).stem}:{node.get('label', nid)}"
    return name


def _edges(result: dict, relation: str | None = None, context: str | None = None):
    name = _named(result)
    return {(name(e["source"]), name(e["target"])) for e in result["edges"]
            if (relation is None or e["relation"] == relation)
            and (context is None or e.get("context") == context)}


def test_typed_member_call_crosses_files(project):
    _, _, r = project
    assert ("game:start()", "board:bind()") in _edges(r, "calls", "call")


def test_static_call_and_static_members(project):
    _, _, r = project
    assert ("game:measure()", "grid:cell_of()") in _edges(r, "calls", "call")
    # Grid.SIZE and Grid.Kind.A both reach the class that declares them, as one
    # edge: one per (source, target, relation), which is all the graph keeps.
    assert ("game:measure()", "grid:Grid") in _edges(r, "references", "member")
    assert sum(1 for e in r["edges"] if e.get("context") == "member"
               and _named(r)(e["source"]) == "game:measure()") == 1


def test_new_references_the_class(project):
    _, _, r = project
    assert ("game:start()", "grid:Grid") in _edges(r, "references", "new")


def test_return_type_carries_the_chain_through_an_operator(project):
    """`n > 0 and _grid.report().add(1)`: the grammar hands the chain over split
    by the operator, and the return type of report() types the receiver of add()."""
    _, _, r = project
    calls = _edges(r, "calls", "call")
    assert ("game:start()", "grid:report()") in calls
    assert ("game:start()", "report:add()") in calls


def test_cast_types_the_receiver(project):
    _, _, r = project
    assert ("special_grid:use()", "grid:report()") in _edges(r, "calls", "call")
    assert ("special_grid:use()", "grid:Grid") in _edges(r, "references", "type")


def test_type_annotations_are_references(project):
    _, _, r = project
    types = _edges(r, "references", "type")
    assert ("game:Game", "board:Board") in types        # @onready var _board: Board
    assert ("game:Game", "grid:Grid") in types          # var _grid: Grid
    assert ("board:bind()", "grid:Grid") in types       # bind(grid: Grid)
    assert ("grid:report()", "report:Report") in types  # -> Report
    assert ("bus:finished (signal)", "report:Report") in types  # signal finished(report: Report)


def test_signal_connected_in_code(project):
    _, _, r = project
    assert ("game:_ready()", "board:picked (signal)") in _edges(r, "references", "connect")
    handlers = _edges(r, "calls", "signal:picked")
    assert ("board:picked (signal)", "game:_on_picked()") in handlers
    assert ("board:picked (signal)", "board:set_hover()") in handlers


def test_autoload_signal_with_a_bound_handler(project):
    _, _, r = project
    assert ("bus:scored (signal)", "game:_on_scored()") in _edges(r, "calls", "signal:scored")
    assert ("game:start()", "bus:scored (signal)") in _edges(r, "references", "emit")


def test_lambda_handler_connects_but_names_no_handler(project):
    _, _, r = project
    assert ("game:_ready()", "bus:finished (signal)") in _edges(r, "references", "connect")
    assert not _edges(r, "calls", "signal:finished")


def test_untyped_receivers_yield_nothing(project):
    """`thing.bind()` and `x.report()` name project methods, but nothing in the
    source says what `thing` or `x` is: an edge would be a guess."""
    _, _, r = project
    assert not {pair for pair in _edges(r) if pair[0].startswith("untyped:poke")}


def test_project_base_is_inherited_and_engine_bases_vanish(project):
    _, _, r = project
    assert _edges(r, "inherits") == {("special_grid:SpecialGrid", "grid:Grid")}
    labels = {n["label"] for n in r["nodes"]}
    assert not labels & {"Node", "Node3D", "RefCounted", "CanvasLayer"}
    assert all(n.get("source_file") for n in r["nodes"])


def test_subclassed_class_keeps_its_global_id(project):
    """Before this pass, `extends Grid` minted a placeholder `grid` node that
    collided with the declaration, and the collision pass salted Grid's id."""
    _, _, r = project
    assert [n["id"] for n in r["nodes"] if n["label"] == "Grid"] == ["grid"]


def test_every_resolved_edge_lands_and_is_extracted(project):
    _, _, r = project
    ids = {n["id"] for n in r["nodes"]}
    resolved = [e for e in r["edges"]
                if e.get("context") in ("call", "new", "member", "type", "connect", "emit", "extends")
                or str(e.get("context", "")).startswith("signal:")]
    assert resolved
    for edge in resolved:
        assert edge["source"] in ids and edge["target"] in ids
        assert edge["confidence"] == "EXTRACTED"


def test_class_name_id_is_not_a_legacy_id(project):
    from graphify.build import build_from_json, graph_has_legacy_ids
    root, _, r = project
    graph = build_from_json(r, root=root)
    nodes = [{"id": nid, **data} for nid, data in graph.nodes(data=True)]
    assert any(n["label"] == "Hud" and n["id"] == "hud" for n in nodes)
    assert not graph_has_legacy_ids(nodes, root=root)


def test_incremental_rebuild_resolves_against_unchanged_files(tmp_path):
    """Only game.gd is re-extracted; the rest of the corpus arrives as context
    nodes, the way graphify update hands it over, and is re-read from disk."""
    paths = _write(tmp_path)
    full = extract(paths, cache_root=tmp_path, root=tmp_path, parallel=False)
    game = tmp_path / "game.gd"
    context_nodes = [
        {k: n.get(k) for k in ("id", "label", "source_file", "file_type")}
        for n in full["nodes"] if not str(n.get("source_file", "")).endswith("game.gd")
    ]
    context_edges = [
        {k: e.get(k) for k in ("source", "target", "relation", "source_file")}
        for e in full["edges"]
        if e["relation"] in ("contains", "method", "inherits")
        and not str(e.get("source_file", "")).endswith("game.gd")
    ]
    partial = extract([game], cache_root=tmp_path, root=tmp_path, parallel=False,
                      resolution_context_nodes=context_nodes,
                      resolution_context_edges=context_edges)
    by_id = {n["id"]: n for n in context_nodes + partial["nodes"]}
    pairs = {(by_id[e["source"]]["label"], by_id[e["target"]]["label"])
             for e in partial["edges"]
             if e["relation"] == "calls" and e["source"] in by_id and e["target"] in by_id}
    assert ("start()", "bind()") in pairs
    assert ("scored (signal)", "_on_scored()") in pairs
