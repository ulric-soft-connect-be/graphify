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


# ── Signal connections ────────────────────────────────────────────────────────

LEGACY_SCENE = GODOT / "scenes" / "legacy.tscn"


def _calls(r):
    by_id = {n["id"]: n for n in r["nodes"]}
    return {(by_id[e["target"]]["label"], e.get("context"))
            for e in r["edges"] if e["relation"] == "calls"}


def test_scene_connection_links_the_scene_to_its_handler():
    """The scene file is the only place naming `_on_button_pressed`; without this
    edge every editor-wired handler in a Godot project reads as dead code."""
    assert ("_on_button_pressed()", "signal:pressed") in _calls(extract_godot_resource(SCENE))


def test_scene_connection_resolves_the_node_path_to_its_script():
    """`to="Data"` is a child node, and its script is set in the property block
    below its own [node] header — not in the header, and not the root's."""
    assert ("_on_data_renamed()", "signal:renamed") in _calls(extract_godot_resource(SCENE))


def test_scene_connection_survives_a_property_wrapping_onto_a_header_shaped_line():
    """`metadata/grid` wraps onto a line reading `[3, 4]`. Read as a section
    header, it would detach the `script =` line that follows from its node."""
    labels = _labels(extract_godot_resource(SCENE))
    assert "_on_data_renamed()" in labels


def test_scene_connection_to_a_node_without_a_script_is_dropped():
    """Nothing in this file says which script defines the handler, and guessing
    the root's would attribute a method to a file that never declares it."""
    assert not any("_on_button_ready" in l for l in _labels(extract_godot_resource(SCENE)))


def test_scene_connection_to_a_method_the_script_does_not_define_is_dropped():
    """Inherited from a base script, or simply stale: either way this file cannot
    name the node id, and a node invented here would claim the wrong source."""
    assert not any("_on_never_defined" in l for l in _labels(extract_godot_resource(SCENE)))


def test_legacy_scene_connection_resolves():
    """Godot 3 writes bare ids (`id=1`, `ExtResource( 1 )`), so the header parser
    has to read unquoted attribute values to tie a node to its script."""
    assert ("_on_button_pressed()", "signal:pressed") in _calls(extract_godot_resource(LEGACY_SCENE))


def test_handler_node_id_is_the_one_the_gdscript_extractor_gives_the_method():
    """The point of the edge is that it lands on the method's real node. A
    different id would add a second, parallel `_on_button_pressed()`."""
    import importlib.util as _ilu
    if _ilu.find_spec("tree_sitter_gdscript") is None:
        import pytest
        pytest.skip("tree-sitter-gdscript not installed")
    from graphify.extract import extract_gdscript
    declared = extract_gdscript(GODOT / "scripts" / "player.gd")
    declared_id = next(n["id"] for n in declared["nodes"] if n["label"] == "_on_button_pressed()")
    r = extract_godot_resource(SCENE)
    assert declared_id in {e["target"] for e in r["edges"] if e["relation"] == "calls"}
