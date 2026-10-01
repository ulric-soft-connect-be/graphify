"""Tests for the GDScript (.gd) extractor."""
from __future__ import annotations
from pathlib import Path
import pytest
from graphify.extract import extract_gdscript

FIXTURES = Path(__file__).parent / "fixtures"
GODOT = FIXTURES / "godot"
PLAYER = GODOT / "scripts" / "player.gd"

import importlib.util as _ilu
_needs_gdscript = pytest.mark.skipif(
    _ilu.find_spec("tree_sitter_gdscript") is None,
    reason="tree-sitter-gdscript not installed",
)

pytestmark = _needs_gdscript


def _labels(r):
    return [n["label"] for n in r["nodes"]]

def _relations(r):
    return {e["relation"] for e in r["edges"]}

def _targets(r, relation):
    by_id = {n["id"]: n for n in r["nodes"]}
    return {by_id[e["target"]]["label"] for e in r["edges"]
            if e["relation"] == relation and e["target"] in by_id}


def test_gdscript_no_error():
    r = extract_gdscript(PLAYER)
    assert "error" not in r


def test_gdscript_finds_class_name():
    assert "FixturePlayer" in _labels(extract_gdscript(PLAYER))


def test_gdscript_finds_functions():
    labels = _labels(extract_gdscript(PLAYER))
    assert "_ready()" in labels
    assert "_advance()" in labels


def test_gdscript_finds_signal():
    assert any("moved" in l for l in _labels(extract_gdscript(PLAYER)))


def test_gdscript_finds_inner_class_and_its_method():
    labels = _labels(extract_gdscript(PLAYER))
    assert "Tracker" in labels
    assert ".record()" in labels


def test_extends_by_name_is_recorded_not_materialized():
    """A base named by type is an engine class or another file's class_name, and
    one file cannot tell which. It is kept on the subclass for the cross-file
    pass (tests/test_gdscript_resolution.py), and no node is minted for it: a
    phantom in the first case, a collision that salts the real class's global id
    in the second."""
    r = extract_gdscript(PLAYER)
    player = next(n for n in r["nodes"] if n["label"] == "FixturePlayer")
    assert player["metadata"]["godot_extends"] == "FixtureBase"
    assert "FixtureBase" not in _labels(r)
    tracker = next(n for n in r["nodes"] if n["label"] == "Tracker")
    assert tracker["metadata"]["godot_extends"] == "RefCounted"
    assert "RefCounted" not in _labels(r)
    assert "inherits" not in _relations(r)


def test_class_name_node_id_is_global():
    """`class_name` registers a project-global name: the declaration's id is the
    bare name, and the node says so for the legacy-id heuristic (#1504)."""
    declared = extract_gdscript(GODOT / "scripts" / "base.gd")
    base = next(n for n in declared["nodes"] if n["label"] == "FixtureBase")
    assert base["id"] == "fixturebase"
    assert base["metadata"]["godot_kind"] == "class_name"


def test_preload_and_resource_path_are_imports():
    r = extract_gdscript(PLAYER)
    targets = _targets(r, "imports_from")
    assert "base.gd" in targets       # const BASE := preload(...)
    assert "water.gdshader" in targets  # const WATER_SHADER := "res://…"


def test_resource_target_is_materialized_as_a_node():
    """A shader is read by no extractor, so without a node of its own the edge
    is pruned as a dangling reference and the dependency disappears (#1327)."""
    r = extract_gdscript(PLAYER)
    shader = next((n for n in r["nodes"] if n["label"] == "water.gdshader"), None)
    assert shader is not None
    assert shader["source_file"].endswith("shaders/water.gdshader")


def test_missing_resource_path_makes_no_edge():
    """`res://` pointing at nothing must not invent a node for a file nobody can open."""
    r = extract_gdscript(PLAYER)
    assert not any("does_not_exist" in l for l in _labels(r))


def test_same_file_call_resolves():
    r = extract_gdscript(PLAYER)
    by_id = {n["id"]: n for n in r["nodes"]}
    pairs = {(by_id[e["source"]]["label"], by_id[e["target"]]["label"])
             for e in r["edges"] if e["relation"] == "calls"}
    assert ("_ready()", "_advance()") in pairs


def test_emit_signal_references_the_signal():
    r = extract_gdscript(PLAYER)
    assert any(e["relation"] == "references" and e.get("context") == "emit_signal"
               for e in r["edges"])


def test_builtins_are_not_call_targets():
    """maxf/print are @GlobalScope functions; resolving them by bare name would
    make a god node out of every math helper a project happens to define."""
    r = extract_gdscript(PLAYER)
    callees = {c["callee"] for c in r["raw_calls"]}
    assert "maxf" not in callees
    assert "print" not in callees
    # A call to another file's function must survive for cross-file resolution.
    assert "shared_helper" in callees


def test_gdscript_no_dangling_edges():
    r = extract_gdscript(PLAYER)
    node_ids = {n["id"] for n in r["nodes"]}
    for e in r["edges"]:
        assert e["source"] in node_ids
        assert e["target"] in node_ids


def test_gd_is_dispatched_and_classified_as_code():
    from graphify.extract import _DISPATCH
    from graphify.detect import CODE_EXTENSIONS
    assert _DISPATCH[".gd"] is extract_gdscript
    assert ".gd" in CODE_EXTENSIONS
