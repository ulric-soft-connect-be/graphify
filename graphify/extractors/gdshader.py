"""Godot shader extractor (.gdshader, .gdshaderinc).

Godot's shading language is GLSL-shaped but not GLSL: it has its own entry points
(``vertex``, ``fragment``, ``light``), its own ``uniform``/``varying`` forms, and
``#include "res://…"``, which is the only way one shader file reaches another. A
regex reader is enough for what the graph needs — the include structure and the
functions — and avoids a grammar dependency for a dialect no grammar targets.
"""
from __future__ import annotations

import re

from pathlib import Path

from graphify.extractors.base import _make_id
from graphify.extractors.godot_paths import resolve_res_path

_INCLUDE_RE = re.compile(r'^\s*#include\s+"([^"]+)"', re.M)

# A function definition at the top level: `vec3 gerstner(vec2 p, float t) {`.
# The parameter list may wrap, but it can hold no `;`, `{` or `)`, which stops the
# match from running across a statement into a later function.
_FUNC_RE = re.compile(
    r"^[ \t]*(?:(?:lowp|mediump|highp|flat|smooth|const|inout|in|out)\s+)*"
    r"([A-Za-z_]\w*)\s+([A-Za-z_]\w*)\s*\(([^;{)]*)\)\s*\{",
    re.M,
)

_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")

# Control-flow keywords read as `<word> <word> (` and would otherwise look like a
# definition (`else if (…) {`) or a call (`for (…)`).
_KEYWORDS = frozenset({
    "if", "else", "for", "while", "do", "switch", "case", "default", "return",
    "break", "continue", "discard", "struct", "uniform", "varying", "const",
    "in", "out", "inout", "shader_type", "render_mode", "group_uniforms",
})

# GLSL built-ins and type constructors. Left in, every shader's calls would
# converge on `mix`, `texture` and `vec3` instead of on the functions the file
# actually defines.
_GLSL_BUILTINS = frozenset({
    "vec2", "vec3", "vec4", "ivec2", "ivec3", "ivec4", "uvec2", "uvec3", "uvec4",
    "bvec2", "bvec3", "bvec4", "mat2", "mat3", "mat4", "float", "int", "uint",
    "bool", "double",
    "abs", "acos", "acosh", "all", "any", "asin", "asinh", "atan", "atanh",
    "ceil", "clamp", "cos", "cosh", "cross", "degrees", "determinant",
    "distance", "dot", "dFdx", "dFdy", "equal", "exp", "exp2", "faceforward",
    "floor", "fract", "fma", "fwidth", "greaterThan", "greaterThanEqual",
    "inverse", "inversesqrt", "isinf", "isnan", "length", "lessThan",
    "lessThanEqual", "log", "log2", "matrixCompMult", "max", "min", "mix",
    "mod", "modf", "normalize", "not", "notEqual", "outerProduct", "pow",
    "radians", "reflect", "refract", "round", "roundEven", "sign", "sin",
    "sinh", "smoothstep", "sqrt", "step", "tan", "tanh", "transpose", "trunc",
    "texture", "textureLod", "textureGrad", "textureProj", "textureSize",
    "texelFetch", "textureGather", "packHalf2x16", "unpackHalf2x16",
    "floatBitsToInt", "floatBitsToUint", "intBitsToFloat", "uintBitsToFloat",
    "bitfieldExtract", "bitfieldInsert", "findLSB", "findMSB", "imageLoad",
    "imageStore", "imageSize", "barrier", "memoryBarrier", "groupMemoryBarrier",
    "atomicAdd", "atomicMin", "atomicMax", "atomicExchange", "atomicCompSwap",
})


def _strip_comments(src: str) -> str:
    """Blank out comments, keeping every newline so line numbers still hold."""
    def repl(m: re.Match) -> str:
        token = m.group(0)
        if token.startswith("/"):
            return "\n" * token.count("\n")
        return token
    return re.sub(r'"(?:\\.|[^"\\])*"|/\*[\s\S]*?\*/|//[^\n]*', repl, src)


def _body_end(src: str, open_brace: int) -> int:
    """Index just past the `}` closing the block that opens at ``open_brace``."""
    depth = 0
    for i in range(open_brace, len(src)):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(src)


def extract_gdshader(path: Path) -> dict:
    """Extract functions, calls, and #include edges from a .gdshader/.gdshaderinc file."""
    try:
        raw_src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"nodes": [], "edges": [], "error": f"cannot read {path}"}

    src = _strip_comments(raw_src)
    str_path = str(path)
    stem = path.with_suffix("").as_posix()
    nodes: list[dict] = []
    edges: list[dict] = []
    node_by_id: dict[str, dict] = {}
    raw_calls: list[dict] = []

    def line_at(offset: int) -> int:
        return src.count("\n", 0, offset) + 1

    def add_node(nid: str, label: str, line: int | None,
                 source_file: str | None = None) -> None:
        if nid in node_by_id:
            return
        entry = {"id": nid, "label": label, "file_type": "code",
                 "source_file": source_file if source_file is not None else str_path,
                 "source_location": f"L{line}" if line else ""}
        nodes.append(entry)
        node_by_id[nid] = entry

    def add_edge(src_id: str, tgt_id: str, relation: str, line: int,
                 context: str | None = None) -> None:
        edge = {"source": src_id, "target": tgt_id, "relation": relation,
                "confidence": "EXTRACTED", "source_file": str_path,
                "source_location": f"L{line}", "weight": 1.0}
        if context:
            edge["context"] = context
        edges.append(edge)

    file_nid = _make_id(str_path)
    add_node(file_nid, path.name, 1)

    # ── #include is the whole dependency structure of a shader tree.
    for m in _INCLUDE_RE.finditer(src):
        target = resolve_res_path(m.group(1), path)
        if target is None:
            continue
        tgt_nid = _make_id(str(target))
        add_node(tgt_nid, target.name, None, source_file=str(target))
        add_edge(file_nid, tgt_nid, "imports_from", line_at(m.start()), context="include")

    # ── Functions, and the calls inside each one.
    functions: list[tuple[str, int, int]] = []   # nid, body start, body end
    for m in _FUNC_RE.finditer(src):
        return_type, name = m.group(1), m.group(2)
        if return_type in _KEYWORDS or name in _KEYWORDS:
            continue
        line = line_at(m.start())
        func_nid = _make_id(stem, name)
        add_node(func_nid, f"{name}()", line)
        add_edge(file_nid, func_nid, "contains", line)
        open_brace = m.end() - 1
        functions.append((func_nid, open_brace, _body_end(src, open_brace)))

    label_to_id = {n["label"]: n["id"] for n in nodes}
    seen_pairs: set[tuple[str, str]] = set()
    for func_nid, start, end in functions:
        for call in _CALL_RE.finditer(src, start, end):
            callee = call.group(1)
            if callee in _KEYWORDS or callee in _GLSL_BUILTINS:
                continue
            tgt_nid = label_to_id.get(f"{callee}()")
            line = line_at(call.start())
            if tgt_nid and tgt_nid != func_nid:
                pair = (func_nid, tgt_nid)
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    add_edge(func_nid, tgt_nid, "calls", line)
            elif not tgt_nid:
                # An included file defines it; cross-file resolution takes it from here.
                raw_calls.append({
                    "caller_nid": func_nid,
                    "callee": callee,
                    "is_member_call": False,
                    "source_file": str_path,
                    "source_location": f"L{line}",
                })

    return {"nodes": nodes, "edges": edges, "raw_calls": raw_calls}
