# This fork: Godot support

A fork of [Graphify-Labs/graphify](https://github.com/Graphify-Labs/graphify) that
reads Godot projects. Upstream reaches no extractor for `.gd`, `.gdshader`,
`.gdshaderinc`, `.tscn` or `.tres`, so a scan of a Godot project finds whatever
Python or shell happens to sit beside the game and none of the game: 8 code files
out of 67 on one add-on, 0 of 225 on a full project.

> This file is specific to the fork. **Remove it before opening a pull request**
> upstream — the rest of the branch is the contribution.

## Install

```
uv tool install --from git+https://github.com/ulric-soft-connect-be/graphify graphifyy --force
```

Or from a local clone: `uv tool install --from ~/Documents/GitHub/graphify graphifyy --force`.

Check it took: `graphify --version`, then in any Godot project
`graphify extract . --code-only` — the line it prints must say `found N code`
with N covering the project's `.gd` files, not a handful.

## What it adds

| File | Reads |
|---|---|
| `graphify/extractors/gdscript.py` | `.gd` — tree-sitter. Classes, inner classes, functions, signals, `extends` (an `inherits` edge), `preload`/`load` and bare `"res://…"` paths (`imports_from`), same-file calls. |
| `graphify/extractors/gdshader.py` | `.gdshader`, `.gdshaderinc` — regex. `#include` edges and function definitions. |
| `graphify/extractors/godot_resource.py` | `.tscn`, `.tres` — regex. `[ext_resource]` edges: which script drives which scene, which sub-scene it instances. `[connection]` edges: the handler an editor-wired signal calls. |
| `graphify/extractors/godot_project.py` | `project.godot` — regex. `[autoload]` singletons, `run/main_scene`, and any other setting holding a `res://` path. |
| `graphify/extractors/godot_paths.py` | `res://` resolution, shared by the four. |

Four decisions carry the result:

- **`class_name` is a project-global name in Godot**, so a declaration's node id is
  global too, and another file's `extends` lands on that exact id with no
  cross-file resolution pass to run.
- **A `res://` target is materialized as a node.** Most of what a Godot script
  loads is a file no extractor reads — a shader, a scene, a mesh — and without a
  node of its own the edge is a dangling reference `build_from_json` prunes
  (upstream #1327), which is how a script's whole dependency on its shaders
  disappears.
- **A signal connection becomes an edge only once the handler is found.**
  `[connection … to="." method="_on_x"]` names a method but not the file that
  declares it, so the extractor resolves the node's script through the scene tree
  and reads it to confirm the `func` is there. A handler inherited from a base
  script, or belonging to an instanced sub-scene, yields no edge rather than a
  node attributed to a file that never declares it.
- **`project.godot` is read for every `res://` value, not for a list of known
  keys.** `[autoload]` and `run/main_scene` are the two that matter — an autoload
  is reached by name from every script, so nothing else in the corpus points at
  its file — but the icon, the default environment and a custom theme are
  dependencies of the same kind, and Godot keeps adding settings of that shape.

Tests: `tests/test_gdscript.py`, `tests/test_godot_shader_scene.py`,
`tests/test_godot_project.py`, fixture Godot project under `tests/fixtures/godot/`.

Measured on a 23-project Godot corpus (9593 nodes, 16861 edges): its 24
`[connection]` sections produce 19 edges, and the five that produce none are
accounted for — three are duplicates (one handler, one signal, several emitters,
collapsed on purpose) and two point at a built-in method on a node carrying no
script. Alongside them, 15 autoloads and 9 `run/main_scene` entry points, none of
which any file in those projects references by path.

## A scene and its script sharing a name lose their edge

Godot's own idiom — `player.tscn` beside `player.gd` — costs the pair its link.
graphify derives a node id from the file path with the extension dropped, so two
files that differ only by extension collapse onto one id, and `build_from_json`
then drops the edges between them as self-edges. The scene keeps every other
`[ext_resource]` edge and loses exactly the one naming the script that drives it,
along with any `[connection]` edge into that script's methods.

This predates the fork: a graph built before these extractors carries the same
composite id and the same missing edge. It lives in the id-remap post-pass, not
in the Godot extractors, and it is not Godot-specific — `foo.ts` beside `foo.tsx`
collides the same way — so fixing it is a separate contribution. On the corpus
above, 19 scene/script pairs are affected.

## Updating from upstream

The fork is shaped so this stays cheap: 1329 lines live in files that do not exist
upstream and can never conflict, against 17 lines touching four upstream files.

```
git fetch origin
git log --oneline HEAD..origin/v8        # what moved; empty means nothing to do
git rebase origin/v8
```

**First, check whether the fork is still needed.** If upstream merged Godot
support (issues #535, #697, #699, #2152 and PRs #1836, #1929, #1242 are all open
as of 2026-09-11), this fork is finished — reinstall the published package
instead:

```
git show origin/v8:graphify/extract.py | grep -c '"\.gd":'
```

Non-zero means upstream now dispatches `.gd`; compare what it extracts against
this fork before deciding, then `uv tool install graphifyy --force`.

**Expect conflicts in exactly two places**, both of them one long line that
upstream rewrites whenever it adds a language of its own:

- `graphify/detect.py` — `CODE_EXTENSIONS`: keep their new list, put
  `'.gd', '.gdshader', '.gdshaderinc', '.tscn', '.tres', '.godot'` back into it.
- `README.md` — the extensions table row: same, and bump the grammar count.

`graphify/extract.py` and `pyproject.toml` add whole lines in sorted blocks, so
they usually rebase clean.

**Then prove it still works**, in this order:

```
uv run pytest tests/test_gdscript.py tests/test_godot_shader_scene.py \
               tests/test_godot_project.py -q                              # 47 tests
uv run pytest tests/ -q                                                    # compare failures against origin/v8, not against zero
```

The full suite is not green on a bare checkout — optional extras (terraform, dm,
ocaml, the `build` module) are not installed by a plain `uv sync`, and they fail
identically before and after this branch. What matters is that the *same* tests
fail on `origin/v8` and on the rebased branch, so measure both.
