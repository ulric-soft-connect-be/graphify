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

Or from a local clone: `uv tool install --from ~/___Projects___/graphify graphifyy --force`.

Check it took: `graphify --version`, then in any Godot project
`graphify extract . --code-only` — the line it prints must say `found N code`
with N covering the project's `.gd` files, not a handful.

## What it adds

| File | Reads |
|---|---|
| `graphify/extractors/gdscript.py` | `.gd` — tree-sitter. Classes, inner classes, functions, signals, `extends` (a path base is an `inherits` edge; a named base is recorded as `metadata.godot_extends`), `preload`/`load` and bare `"res://…"` paths (`imports_from`), same-file calls. |
| `graphify/extractors/gdscript_resolution.py` | `.gd`, cross-file pass registered in `resolver_registry`. Typed member calls, `X.new()`, static members, type annotations, signals connected and emitted in code, autoloads, and `inherits` for a project base class. See below. |
| `graphify/extractors/gdshader.py` | `.gdshader`, `.gdshaderinc` — regex. `#include` edges and function definitions. |
| `graphify/extractors/godot_resource.py` | `.tscn`, `.tres` — regex. `[ext_resource]` edges: which script drives which scene, which sub-scene it instances. `[connection]` edges: the handler an editor-wired signal calls. |
| `graphify/extractors/godot_project.py` | `project.godot` — regex. `[autoload]` singletons, `run/main_scene`, and any other setting holding a `res://` path. |
| `graphify/extractors/godot_paths.py` | `res://` resolution, shared by the four. |

Five decisions carry the result:

- **`class_name` is a project-global name in Godot**, so a declaration's node id is
  global too (`hud` for `class_name Hud`), and the node is marked
  `metadata.godot_kind = "class_name"`. Upstream's legacy-id heuristic
  (`graph_has_legacy_ids`, #1504) otherwise reads `hud` in `src/hud/hud.gd` as a
  pre-#1504 bare-filename stem, and `graphify query` warns on every Godot graph.
  `_has_global_id` in `build.py` exempts the marked nodes.
- **A base class named by type gets no node from the per-file extractor.** It is an
  engine class (`Node3D`) or a `class_name` declared in another file, and one file
  cannot tell which. The extractor used to mint a source-less placeholder, which
  left five phantom engine nodes bridging unrelated communities on a 20-script
  project. Worse, when the base was a project class, the placeholder collided with
  the real declaration, and `_disambiguate_colliding_node_ids` salted the class away
  from its global id (`minefield` became `src_field_mine_field_gd_minefield`). The
  name now rides on the subclass (`metadata.godot_extends`), and the cross-file
  pass emits `inherits` only when a project file declares it.
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
`tests/test_godot_project.py`, `tests/test_godot_stem_collision.py`,
`tests/test_gdscript_resolution.py`, fixture Godot project under
`tests/fixtures/godot/`.

Measured on a 24-project Godot corpus (10013 nodes, 18645 edges, upstream 0.9.73):
its 24 `[connection]` sections produce 19 edges, and the five that produce none are
accounted for — three are duplicates (one handler, one signal, several emitters,
collapsed on purpose) and two point at a built-in method on a node carrying no
script. Alongside them, 15 autoloads and 12 `run/main_scene` entry points, none of
which any file in those projects references by path.

## Cross-file resolution

`extract_gdscript` reads one file and resolves calls into that file only. A Godot
script reaches another file's code through a `class_name`, an autoload, or a
variable typed as one of those. `gdscript_resolution` resolves exactly that, and
nothing it would have to guess:

- **The receiver's type must be stated in the source**: a type annotation (on a
  member, a local, a parameter, a `for` variable), `X.new()`, `expr as X`, a
  `preload("res://x.gd")` constant, an autoload name, or the declared return type
  of a project method (`game.board().chunk_view_at(cell)`). An untyped receiver
  yields no edge. Every edge is EXTRACTED, upstream's convention for a receiver
  type the source names (#1533).
- **Signals wired in code** are the same fact as a scene's `[connection]`, so they
  take the same shape. `x.sig.connect(handler)` gives `references / connect` from
  the wiring function to the signal, and `calls / signal:<name>` from the signal to
  the handler. Handlers may be a bare method, `obj.method`, or `method.bind(…)`;
  a lambda names no handler. `x.sig.emit(…)` gives `references / emit`.
- **Type references** (`references / type`) go from the function, class or signal
  that names a project class in a type (`var x: T`, `-> T`, `Array[T]`,
  `signal s(a: T)`, `as T`, `is T`). Static members (`T.CONST`, `T.Enum.VALUE`)
  give `references / member`, `T.new()` gives `references / new`.
- **One edge per (source, target, relation)**, the way upstream's member-call
  resolvers dedupe, and the most an undirected graph keeps anyway.
- **tree-sitter-gdscript splits a chain after an operator.** `a and s.f.g(c)` parses
  as `(a and s.f).g(c)`, and so do `==`, `or`, `+`, `not` and unary `-`. A chain
  cannot really start with a bare operator expression (that needs parentheses),
  so `_reassociate` rebuilds it. Without it, every `if x and state.field.is_mine()`
  lost its call.
- **Incremental rebuilds** hand the pass the unchanged corpus's nodes, but only a
  whitelist of metadata keys travels with them (`watch.py`, `cli.py`), and member
  types are not among them. Rather than widen both upstream whitelists, the pass
  re-reads an unchanged script from disk for its declarations. Edges still come
  only from the re-extracted files.

Measured on mine-horizon (20 scripts, 2026-10-01). Before the pass, the controller
that creates and queries the pure layer on every move had no edge to it, and about
twenty methods called only from other files looked dead. After it, 200 resolved
edges were read one by one against the source, and none was wrong. In detail:
97 typed calls, 42 type references, 15 static members, 12 `new`, 12 connects,
10 signal handlers, 7 emits, and the 4 calls the operator split had hidden.

What stays out, on purpose: a property read (`stats.lives`) is not an edge, a call
on an engine type has no target in the corpus, and a receiver typed only by
`get_node()`/`$Path` is untyped.

**The fork's version carries a local label (`0.9.73+godot.N`), and the label must
move whenever an extractor's output changes.** The AST cache is namespaced by
package version (`cache/ast/v{version}-s{schema}/`). If the fork changes what
`extract_gdscript` returns under an unchanged version, every Godot project that
was already analysed replays its old per-file results silently: phantom nodes
included, and the new pass running on top of them.

## A scene and its script sharing a name

Godot's own idiom — `player.tscn` beside `player.gd` — puts two files on one id:
graphify derives a node id from the file path with the extension dropped, and
`_disambiguate_colliding_node_ids` then salts the pair apart by path
(`player_tscn_player`, `player_gd_player`). An edge *into* the pair still names the
bare `player`, so on its own it either becomes a self-loop (the scene naming its
own script, which `build_from_json` drops) or stays on the dead shared id — which
since upstream 0.9.63 (#2873) is minted as an `external` stub, a phantom standing
for a file that sits in the project.

Every `imports_from` edge the Godot extractors emit therefore carries the resolved
file as `target_file`, the hint that pass already reads for `foo.ts` beside
`foo.mjs` (#1814) and pops before anything is written. On the corpus above that is
193 edges — 95 from a scene to its own same-stem script, 98 into a colliding pair
from elsewhere — that were otherwise dropped or sent to a phantom.

One case stays open: `extends "res://enemy.gd"` is an `inherits` edge, and the
pass reads `target_file` only for `imports`, `imports_from` and `re_exports`. The
`imports_from` edge emitted alongside it does land; the `inherits` one, when
`enemy.tscn` also exists, does not. Fixing that means widening upstream's relation
list, and the corpus holds three path-based `extends`, none of them on a colliding
pair, so it is left alone.

## Updating from upstream

The fork is shaped so this stays cheap: about 2,480 lines live in files that do not
exist upstream and can never conflict, against 25 lines touching five upstream
files (`detect.py`, `README.md`, `extract.py`, `build.py`, `pyproject.toml`).

```
git fetch origin
git log --oneline HEAD..origin/v8        # what moved; empty means nothing to do
git rebase origin/v8
```

If `git fetch` dies on `bad object refs/remotes/fork/gdscript-extractor 2`, a
file-sync or Finder copy duplicated a ref inside `.git` — the name with a space is
never one git writes. Delete that one file (`rm ".git/refs/remotes/fork/gdscript-extractor 2"`)
and fetch again.

**First, check whether the fork is still needed.** If upstream merged Godot
support (issues #535, #697, #699, #2152 and PRs #1929, #1242, #3750 are all open
as of 2026-10-01; #1836 was closed unmerged), this fork is finished — reinstall
the published package instead:

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

`graphify/extract.py` adds whole lines (an import in the sorted extractor block,
one `register_language_resolver` after the markdown one), and `graphify/build.py`
adds three lines to `_has_global_id`. Both usually rebase clean. `pyproject.toml`
conflicts on the `version` line at every upstream release: take upstream's number
and keep the local label (`0.9.80+godot.N`), bumping N if the rebase changed what
an extractor returns.

**Then prove it still works**, in this order:

```
uv run pytest tests/test_gdscript.py tests/test_godot_shader_scene.py \
               tests/test_godot_project.py tests/test_godot_stem_collision.py \
               tests/test_gdscript_resolution.py -q                            # 69 tests
uv run pytest tests/ -q                                                    # compare failures against origin/v8, not against zero
```

The full suite is not green on a bare checkout — tests for optional extras a
plain `uv sync` does not install fail identically before and after this branch
(as of 0.9.73: 25 tests across erlang, r, solidity, vbnet and ollama-retry). What
matters is that the *same* tests fail on `origin/v8` and on the rebased branch, so
measure both.

`uv sync` rewrites `uv.lock` to add `tree-sitter-gdscript`. Leave that out of the
commits (`git checkout uv.lock` after testing): `uv tool install` does not read the
lockfile, and a committed one would conflict on every upstream release.
