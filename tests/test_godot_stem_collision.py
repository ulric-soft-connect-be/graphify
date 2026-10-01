"""A Godot file and its same-stem sibling keep the edges between them.

Godot's own idiom puts ``enemy.tscn`` beside ``enemy.gd``. A file id drops the
extension, so both collapse onto ``enemy`` and ``_disambiguate_colliding_node_ids``
salts them apart by path (``enemy_tscn_enemy`` / ``enemy_gd_enemy``). An edge INTO
that pair names the bare id, though, and without the resolved file to key on it
either stays on the dead id — minted as an ``external`` stub since #2873, though the
file sits in the project — or turns into a self-loop when the scene names its own
script. The extractors stamp ``target_file`` (#1814) so the edge lands on the right
sibling.
"""
from __future__ import annotations

from pathlib import Path

from graphify.extract import extract

_GD = (".gd", ".gdshader", ".gdshaderinc", ".tscn", ".tres", ".godot")

_FILES = {
    "project.godot": (
        'config_version=5\n\n[application]\n\nrun/main_scene="res://main.tscn"\n'
    ),
    "main.tscn": (
        '[gd_scene load_steps=3 format=3]\n\n'
        '[ext_resource type="Script" path="res://main.gd" id="1"]\n'
        '[ext_resource type="PackedScene" path="res://enemy.tscn" id="2"]\n\n'
        '[node name="Main" type="Node2D"]\nscript = ExtResource("1")\n\n'
        '[node name="Enemy" parent="." instance=ExtResource("2")]\n'
    ),
    "main.gd": (
        'extends Node2D\n\nconst Enemy = preload("res://enemy.tscn")\n\n'
        'func _ready():\n\tpass\n'
    ),
    "enemy.tscn": (
        '[gd_scene load_steps=2 format=3]\n\n'
        '[ext_resource type="Script" path="res://enemy.gd" id="1"]\n\n'
        '[node name="Enemy" type="Area2D"]\nscript = ExtResource("1")\n\n'
        '[connection signal="body_entered" from="." to="." method="_on_body_entered"]\n'
    ),
    "enemy.gd": "extends Area2D\n\nfunc _on_body_entered(body):\n\tpass\n",
    "water.gdshader": (
        'shader_type canvas_item;\n\n#include "res://water.gdshaderinc"\n\n'
        'void fragment() {\n\tCOLOR = tint();\n}\n'
    ),
    "water.gdshaderinc": "vec4 tint() {\n\treturn vec4(1.0);\n}\n",
}


def _extract(tmp_path: Path) -> dict:
    paths = []
    for name, text in _FILES.items():
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return extract(paths, cache_root=tmp_path, root=tmp_path, parallel=False)


def _file_id(result: dict, name: str) -> str:
    ids = [n["id"] for n in result["nodes"]
           if n.get("label") == name and str(n.get("source_file", "")).endswith(name)]
    assert len(set(ids)) == 1, f"expected one node for {name}; got {ids}"
    return ids[0]


def _imports(result: dict, source: str) -> dict[str, str]:
    """context -> target id, for the imports_from edges leaving ``source``."""
    return {e.get("context", ""): e["target"] for e in result["edges"]
            if e["source"] == source and e["relation"] == "imports_from"}


def test_scene_reaches_its_own_same_stem_script(tmp_path):
    result = _extract(tmp_path)
    enemy_tscn = _file_id(result, "enemy.tscn")
    assert _imports(result, enemy_tscn)["script"] == _file_id(result, "enemy.gd")


def test_scene_instancing_a_colliding_scene_reaches_that_scene(tmp_path):
    result = _extract(tmp_path)
    main_tscn = _file_id(result, "main.tscn")
    deps = _imports(result, main_tscn)
    assert deps["packedscene"] == _file_id(result, "enemy.tscn")
    assert deps["script"] == _file_id(result, "main.gd")


def test_script_preloading_a_colliding_scene_reaches_that_scene(tmp_path):
    result = _extract(tmp_path)
    main_gd = _file_id(result, "main.gd")
    assert _imports(result, main_gd)["preload"] == _file_id(result, "enemy.tscn")


def test_main_scene_entry_point_reaches_the_scene_not_its_script(tmp_path):
    result = _extract(tmp_path)
    project = _file_id(result, "project.godot")
    assert _imports(result, project)["run/main_scene"] == _file_id(result, "main.tscn")


def test_shader_including_its_same_stem_include_reaches_it(tmp_path):
    result = _extract(tmp_path)
    shader = _file_id(result, "water.gdshader")
    assert _imports(result, shader)["include"] == _file_id(result, "water.gdshaderinc")


def test_signal_handler_edge_survives_the_collision(tmp_path):
    result = _extract(tmp_path)
    enemy_tscn = _file_id(result, "enemy.tscn")
    handlers = {e["target"] for e in result["edges"]
                if e["source"] == enemy_tscn and e["relation"] == "calls"}
    by_id = {n["id"]: n for n in result["nodes"]}
    assert [by_id[h]["label"] for h in handlers] == ["_on_body_entered()"]
    assert by_id[next(iter(handlers))]["source_file"].endswith("enemy.gd")


def test_no_godot_edge_is_a_self_loop_or_lands_off_the_file_nodes(tmp_path):
    result = _extract(tmp_path)
    by_id = {n["id"]: n for n in result["nodes"]}
    godot_edges = [e for e in result["edges"]
                   if e["relation"] == "imports_from"
                   and str(e.get("source_file", "")).endswith(_GD)]
    assert godot_edges
    for e in godot_edges:
        assert e["source"] != e["target"], e
        assert by_id.get(e["target"], {}).get("source_file"), e
        assert "target_file" not in e   # the hint is consumed, never written out
