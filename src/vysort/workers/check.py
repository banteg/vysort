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
import math
import pathlib
import random
import sys

from vyper.compiler.phases import CompilerData

ENUMERATION_CAP = 20000


def call_graph(data: CompilerData):
    """Read the function list passed to _topsort, in source order."""
    callees, internal = {}, set()
    for f in data.global_ctx._function_defs:
        typ = f._metadata["type"]
        callees[f.name] = [c.name for c in typ.called_functions]
        if typ.is_internal:
            internal.add(f.name)
    return callees, internal


def simulate(callee_order, internal):
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

    for f in callee_order:
        helper(f)
    return tuple(f for f in placed if f in internal)


def decision_points(callee_order, internal):
    """Count sets that, at first visit, contain >=2 unplaced callees."""
    points = []
    placed = set()

    def helper(f):
        if f in placed:
            return
        unplaced = [c for c in callee_order[f] if c not in placed]
        if sum(c in internal for c in unplaced) >= 2:
            points.append((f, len(unplaced)))
        for c in callee_order[f]:
            helper(c)
        placed.add(f)

    for f in callee_order:
        helper(f)
    return points


def enumerate_layouts(callee_order, internal, env_layout, decisions):
    """Enumerate per-set permutations (cartesian product), cap with sampling.

    Returns (layouts, exact, choice_space)."""
    multi = [n for n, c in callee_order.items() if len(c) >= 2]
    space = math.prod(math.factorial(len(callee_order[n])) for n in multi)
    if not decisions:
        # Permuting already-placed callees cannot change the DFS output.
        return {env_layout}, True, space
    layouts = set()
    if space <= ENUMERATION_CAP:
        pools = [list(itertools.permutations(callee_order[n])) for n in multi]
        for combo in itertools.product(*pools):
            co = dict(callee_order)
            co.update({n: list(p) for n, p in zip(multi, combo)})
            layouts.add(simulate(co, internal))
        return layouts, True, space
    rng = random.Random(1)
    for _ in range(ENUMERATION_CAP):
        co = dict(callee_order)
        for n in multi:
            p = callee_order[n][:]
            rng.shuffle(p)
            co[n] = p
        layouts.add(simulate(co, internal))
    return layouts, False, space


def check(src: str) -> dict:
    callees, internal = call_graph(CompilerData(src))
    env_layout = simulate(callees, internal)
    dps = decision_points(callees, internal)
    layouts, exact, space = enumerate_layouts(callees, internal, env_layout, dps)

    return {
        "internal_fns": len(internal),
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
