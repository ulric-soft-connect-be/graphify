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
| `graphify/extractors/godot_resource.py` | `.tscn`, `.tres` — regex. `[ext_resource]` edges: which script drives which scene. |
| `graphify/extractors/godot_paths.py` | `res://` resolution, shared by the three. |

Two decisions carry the result:

- **`class_name` is a project-global name in Godot**, so a declaration's node id is
  global too, and another file's `extends` lands on that exact id with no
  cross-file resolution pass to run.
- **A `res://` target is materialized as a node.** Most of what a Godot script
  loads is a file no extractor reads — a shader, a scene, a mesh — and without a
  node of its own the edge is a dangling reference `build_from_json` prunes
  (upstream #1327), which is how a script's whole dependency on its shaders
  disappears.

Tests: `tests/test_gdscript.py`, `tests/test_godot_shader_scene.py`, fixture Godot
project under `tests/fixtures/godot/`.

## Updating from upstream

The fork is shaped so this stays cheap: 964 lines live in files that do not exist
upstream and can never conflict, against 13 lines touching four upstream files.

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
  `'.gd', '.gdshader', '.gdshaderinc', '.tscn', '.tres'` back into it.
- `README.md` — the extensions table row: same, and bump the grammar count.

`graphify/extract.py` and `pyproject.toml` add whole lines in sorted blocks, so
they usually rebase clean.

**Then prove it still works**, in this order:

```
uv run pytest tests/test_gdscript.py tests/test_godot_shader_scene.py -q   # 31 tests
uv run pytest tests/ -q                                                    # compare failures against origin/v8, not against zero
```

The full suite is not green on a bare checkout — optional extras (terraform, dm,
ocaml, the `build` module) are not installed by a plain `uv sync`, and they fail
identically before and after this branch. What matters is that the *same* tests
fail on `origin/v8` and on the rebased branch, so measure both.
