"""Decision-point analysis for vyper 0.3.4-0.3.7 ordering nondeterminism.

The bug requires multiple valid topsorts — i.e. some called_functions set
holding >=2 not-yet-placed internal functions at DFS time. Contracts without
such a decision point have exactly one reachable layout and are immune.

Captures the call graph + source order from a real compile, enumerates (or
samples) all per-set iteration orders, and counts distinct reachable internal
layouts.

Self-contained worker: runs in an ephemeral `uv run --with vyper==X` env and
imports nothing from vysort. argv: <source.vy>; JSON report on stdout.
"""

import itertools
import json
import pathlib
import random
import sys

import vyper.codegen.module as module_mod
from vyper.compiler.phases import CompilerData

ENUMERATION_CAP = 20000


def capture_call_graph(src: str) -> dict:
    """Compile once with an instrumented _topsort; return the call graph."""
    captured = {}
    orig_topsort = module_mod._topsort

    def capturing_topsort(functions):
        if not captured:
            for f in functions:
                t = f._metadata["type"]
                captured[f.name] = {
                    "callees": [c.name for c in t.called_functions],  # env iteration order
                    "internal": t.is_internal,
                }
            captured["__source_order__"] = [f.name for f in functions]
        return orig_topsort(functions)

    module_mod._topsort = capturing_topsort
    try:
        CompilerData(src).bytecode_runtime  # triggers codegen -> capture
    finally:
        module_mod._topsort = orig_topsort
    return captured


def simulate(source_order, callee_order, internal):
    """Replicate _topsort + dedup-first; return internal layout tuple."""
    placed = []
    placed_set = set()

    def helper(f):
        if f in placed_set:
            return
        for c in callee_order[f]:
            helper(c)
        if f not in placed_set:
            placed.append(f)
            placed_set.add(f)

    for f in source_order:
        helper(f)
    return tuple(f for f in placed if internal.get(f))


def decision_points(source_order, callee_order, internal):
    """Count sets that, at first visit, contain >=2 unplaced callees."""
    points = []
    placed = set()

    def helper(f):
        if f in placed:
            return
        unplaced = [c for c in callee_order[f] if c not in placed]
        if len([c for c in unplaced if internal.get(c)]) >= 2:
            points.append((f, len(unplaced)))
        for c in callee_order[f]:
            helper(c)
        placed.add(f)

    for f in source_order:
        helper(f)
    return points


def check(src: str) -> dict:
    captured = capture_call_graph(src)
    source_order = captured.pop("__source_order__")
    internal = {n: v["internal"] for n, v in captured.items()}
    base_callees = {n: v["callees"] for n, v in captured.items()}

    env_layout = simulate(source_order, base_callees, internal)
    dps = decision_points(source_order, base_callees, internal)

    # enumerate per-set permutations (cartesian product), cap with sampling
    multi = [n for n, c in base_callees.items() if len(c) >= 2]
    space = 1
    for n in multi:
        for i in range(2, len(base_callees[n]) + 1):
            space *= i
    layouts = set()
    if space <= ENUMERATION_CAP:
        pools = [list(itertools.permutations(base_callees[n])) for n in multi]
        for combo in itertools.product(*pools):
            co = dict(base_callees)
            co.update({n: list(p) for n, p in zip(multi, combo)})
            layouts.add(simulate(source_order, co, internal))
        exact = True
    else:
        rng = random.Random(1)
        for _ in range(ENUMERATION_CAP):
            co = dict(base_callees)
            for n in multi:
                p = base_callees[n][:]
                rng.shuffle(p)
                co[n] = p
            layouts.add(simulate(source_order, co, internal))
        exact = False

    return {
        "internal_fns": sum(internal.values()),
        "decision_points": len(dps),
        "choice_space": space,
        "reachable_layouts": len(layouts),
        "reachable_exact": exact,
        "immune": len(layouts) == 1 and exact,
        "env_layout": list(env_layout),
        "layouts": sorted(layouts) if exact and len(layouts) <= 64 else None,
    }


if __name__ == "__main__":
    src = pathlib.Path(sys.argv[1]).read_text()
    print(json.dumps(check(src), indent=2))
