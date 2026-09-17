"""Tests for the Godot project file extractor (project.godot)."""
from __future__ import annotations
from pathlib import Path
from graphify.extract import extract_godot_project

FIXTURES = Path(__file__).parent / "fixtures"
GODOT = FIXTURES / "godot"
PROJECT = GODOT / "project.godot"


def _labels(r):
    return [n["label"] for n in r["nodes"]]

def _imports(r):
    by_id = {n["id"]: n for n in r["nodes"]}
    return {(by_id[e["target"]]["label"], e.get("context"))
            for e in r["edges"] if e["relation"] == "imports_from"}


def test_project_no_error():
    assert "error" not in extract_godot_project(PROJECT)


def test_main_scene_is_an_import():
    """`run/main_scene` is the root of the whole scene tree — the entry point of
    the project, and nothing else in the corpus points at it."""
    assert ("main.tscn", "run/main_scene") in _imports(extract_godot_project(PROJECT))


def test_autoload_edge_carries_the_global_name():
    """An autoload is reached from every script by the name declared here, so the
    key is the part of the setting worth keeping."""
    assert ("base.gd", "autoload:FixtureSingleton") in _imports(extract_godot_project(PROJECT))


def test_autoload_without_the_enabled_star_still_resolves():
    assert ("player.gd", "autoload:Disabled") in _imports(extract_godot_project(PROJECT))


def test_missing_autoload_target_makes_no_node():
    assert not any("does_not_exist" in l for l in _labels(extract_godot_project(PROJECT)))


def test_non_path_settings_are_not_edges():
    """`config/name` is a string like any other; only a res:// value is a file."""
    assert not any("graphify gdscript fixture" in l for l in _labels(extract_godot_project(PROJECT)))


def test_project_no_dangling_edges():
    r = extract_godot_project(PROJECT)
    node_ids = {n["id"] for n in r["nodes"]}
    for e in r["edges"]:
        assert e["source"] in node_ids and e["target"] in node_ids


def test_autoload_target_is_the_node_the_script_extractor_declares():
    """The edge has to land on the script's own file node, not a second one."""
    from graphify.extractors.base import _make_id
    base = GODOT / "scripts" / "base.gd"
    r = extract_godot_project(PROJECT)
    assert _make_id(str(base)) in {e["target"] for e in r["edges"]}


def test_godot_suffix_is_dispatched_and_classified_as_code():
    from graphify.extract import _DISPATCH
    from graphify.detect import CODE_EXTENSIONS
    assert _DISPATCH[".godot"] is extract_godot_project
    assert ".godot" in CODE_EXTENSIONS
