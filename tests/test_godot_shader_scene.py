"""Tests for the Godot shader (.gdshader/.gdshaderinc) and scene (.tscn/.tres) extractors."""
from __future__ import annotations
from pathlib import Path
from graphify.extract import extract_gdshader, extract_godot_resource

FIXTURES = Path(__file__).parent / "fixtures"
GODOT = FIXTURES / "godot"
SHADER = GODOT / "shaders" / "water.gdshader"
SCENE = GODOT / "scenes" / "main.tscn"


def _labels(r):
    return [n["label"] for n in r["nodes"]]

def _targets(r, relation):
    by_id = {n["id"]: n for n in r["nodes"]}
    return {by_id[e["target"]]["label"] for e in r["edges"]
            if e["relation"] == relation and e["target"] in by_id}


# ── Shaders ───────────────────────────────────────────────────────────────────

def test_shader_no_error():
    assert "error" not in extract_gdshader(SHADER)


def test_shader_include_is_an_import():
    assert "common.gdshaderinc" in _targets(extract_gdshader(SHADER), "imports_from")


def test_shader_missing_include_makes_no_node():
    assert not any("does_not_exist" in l for l in _labels(extract_gdshader(SHADER)))


def test_shader_finds_functions_including_a_wrapped_signature():
    labels = _labels(extract_gdshader(SHADER))
    assert "fragment()" in labels
    assert "surface_colour()" in labels   # its parameter list spans three lines


def test_shader_ignores_definitions_named_inside_comments():
    """A comment mentioning fragment() must not add a second node for it."""
    assert _labels(extract_gdshader(SHADER)).count("fragment()") == 1


def test_shader_same_file_call_resolves():
    r = extract_gdshader(SHADER)
    by_id = {n["id"]: n for n in r["nodes"]}
    pairs = {(by_id[e["source"]]["label"], by_id[e["target"]]["label"])
             for e in r["edges"] if e["relation"] == "calls"}
    assert ("fragment()", "surface_colour()") in pairs


def test_shader_call_into_an_include_is_left_for_cross_file_resolution():
    r = extract_gdshader(SHADER)
    assert "wave_height" in {c["callee"] for c in r["raw_calls"]}


def test_shader_glsl_builtins_are_not_call_targets():
    """mix/sin/vec3 are in every shader; resolving them by name makes god nodes."""
    callees = {c["callee"] for c in extract_gdshader(SHADER)["raw_calls"]}
    assert not ({"mix", "sin", "vec3", "if"} & callees)


def test_gdshaderinc_is_extracted_too():
    r = extract_gdshader(GODOT / "shaders" / "common.gdshaderinc")
    assert "wave_height()" in _labels(r)


# ── Scenes and resources ──────────────────────────────────────────────────────

def test_scene_no_error():
    assert "error" not in extract_godot_resource(SCENE)


def test_scene_links_to_its_scripts():
    targets = _targets(extract_godot_resource(SCENE), "imports_from")
    assert "player.gd" in targets
    assert "base.gd" in targets


def test_scene_edge_carries_the_resource_type():
    r = extract_godot_resource(SCENE)
    assert any(e.get("context") == "script" for e in r["edges"])


def test_scene_names_a_script_twice_but_depends_on_it_once():
    r = extract_godot_resource(SCENE)
    by_id = {n["id"]: n for n in r["nodes"]}
    player_edges = [e for e in r["edges"] if by_id[e["target"]]["label"] == "player.gd"]
    assert len(player_edges) == 1


def test_scene_missing_resource_makes_no_edge():
    assert not any("missing.png" in l for l in _labels(extract_godot_resource(SCENE)))


def test_scene_no_dangling_edges():
    r = extract_godot_resource(SCENE)
    node_ids = {n["id"] for n in r["nodes"]}
    for e in r["edges"]:
        assert e["source"] in node_ids and e["target"] in node_ids


def test_godot_suffixes_are_dispatched_and_classified_as_code():
    from graphify.extract import _DISPATCH
    from graphify.detect import CODE_EXTENSIONS
    assert _DISPATCH[".gdshader"] is extract_gdshader
    assert _DISPATCH[".gdshaderinc"] is extract_gdshader
    assert _DISPATCH[".tscn"] is extract_godot_resource
    assert _DISPATCH[".tres"] is extract_godot_resource
    for ext in (".gdshader", ".gdshaderinc", ".tscn", ".tres"):
        assert ext in CODE_EXTENSIONS
