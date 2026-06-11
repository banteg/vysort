# /// script
# requires-python = ">=3.9,<3.12"
# dependencies = ["vyper==0.3.7"]
# ///
"""Force a vanilla vyper 0.3.x compiler into a specific internal-function
layout by reordering source definitions: move the targeted internal function
defs to the front (before any other function def), ordered by the target
layout. With internals defined before every caller, the topsort has zero
decision points and the layout is deterministic in any environment.

Uses vyper's own AST for exact function spans (handles multi-line signatures).

Usage: reorder_source.py <source.vy> <layout.json> > reordered.vy
layout.json: ["_f1", "_f2", ...] (target internal layout order)
"""
import json
import pathlib
import sys

import vyper.ast as vy_ast


def reorder(src, layout):
    lines = src.splitlines(keepends=True)
    mod = vy_ast.parse_to_ast(src)
    spans = {}  # name -> (start_line0, end_line0_exclusive)
    first_def_start = None
    for f in mod.get_children(vy_ast.FunctionDef):
        start = min([d.lineno for d in f.decorator_list] + [f.lineno]) - 1
        # include contiguous leading comment lines so moved functions keep
        # their banner comments/NatSpec (cosmetic; bytecode is unaffected)
        while start > 0 and lines[start - 1].strip().startswith("#"):
            start -= 1
        end = f.end_lineno
        spans[f.name] = (start, end)
        if first_def_start is None or start < first_def_start:
            first_def_start = start
    missing = [n for n in layout if n not in spans]
    if missing:
        raise SystemExit(f"functions not found in source: {missing}")

    moved = set()
    for n in layout:
        moved.update(range(*spans[n]))

    out = []
    for i, line in enumerate(lines):
        if i == first_def_start:
            for n in layout:
                s, e = spans[n]
                out.extend(lines[s:e])
                out.append("\n\n")
        if i not in moved:
            out.append(line)
    return "".join(out)


if __name__ == "__main__":
    src = pathlib.Path(sys.argv[1]).read_text()
    layout = json.loads(pathlib.Path(sys.argv[2]).read_text())
    sys.stdout.write(reorder(src, layout))
