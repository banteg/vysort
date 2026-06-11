# /// script
# requires-python = ">=3.9,<3.12"
# dependencies = ["vyper==0.3.7"]
# ///
"""Test the theory: 0.3.7 ordering nondeterminism requires multiple valid
topsorts — i.e. some called_functions set holding >=2 not-yet-placed internal
functions at DFS time. Contracts without such a decision point have exactly
one reachable layout and are immune.

For each contract: capture the call graph + source order, enumerate (or
sample) all per-set iteration orders, count distinct reachable internal
layouts, and compare with the deployed order recovered from on-chain code.

Usage: theory_check.py <source.vy> [<onchain_runtime.hex>]
Prints one JSON line.
"""
import itertools
import json
import pathlib
import random
import sys

from vyper.compiler.phases import CompilerData
import vyper.codegen.module as module_mod

CAPTURE = {}
_orig_topsort = module_mod._topsort
def capturing_topsort(functions):
    if not CAPTURE:
        for f in functions:
            t = f._metadata["type"]
            CAPTURE[f.name] = {
                "callees": [c.name for c in t.called_functions],  # env iteration order
                "internal": t.is_internal,
            }
        CAPTURE["__source_order__"] = [f.name for f in functions]
    return _orig_topsort(functions)
module_mod._topsort = capturing_topsort


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


def main():
    src = pathlib.Path(sys.argv[1]).read_text()
    data = CompilerData(src)
    _ = data.bytecode_runtime  # triggers codegen -> capture

    source_order = CAPTURE.pop("__source_order__")
    internal = {n: v["internal"] for n, v in CAPTURE.items()}
    base_callees = {n: v["callees"] for n, v in CAPTURE.items()}
    n_internal = sum(internal.values())

    env_layout = simulate(source_order, base_callees, internal)
    dps = decision_points(source_order, base_callees, internal)

    # enumerate per-set permutations (cartesian product), cap with sampling
    multi = [n for n, c in base_callees.items() if len(c) >= 2]
    space = 1
    for n in multi:
        for i in range(2, len(base_callees[n]) + 1):
            space *= i
    CAP = 20000
    layouts = set()
    if space <= CAP:
        pools = [list(itertools.permutations(base_callees[n])) for n in multi]
        for combo in itertools.product(*pools):
            co = dict(base_callees)
            co.update({n: list(p) for n, p in zip(multi, combo)})
            layouts.add(simulate(source_order, co, internal))
        exact = True
    else:
        rng = random.Random(1)
        for _ in range(CAP):
            co = dict(base_callees)
            for n in multi:
                p = base_callees[n][:]
                rng.shuffle(p)
                co[n] = p
            layouts.add(simulate(source_order, co, internal))
        exact = False

    out = {
        "internal_fns": n_internal,
        "decision_points": len(dps),
        "choice_space": space,
        "reachable_layouts": len(layouts),
        "reachable_exact": exact,
        "env_layout": list(env_layout),
    }

    if len(sys.argv) > 2:
        # recover deployed layout from on-chain code (reuse layout_recover2)
        sys.argv = [sys.argv[0], sys.argv[1], sys.argv[2]]
        import subprocess
        r = subprocess.run(
            [sys.executable, str(pathlib.Path(__file__).parent / "layout_recover2.py"),
             sys.argv[1], sys.argv[2]],
            capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if line.startswith("RESULT "):
                res = json.loads(line[7:])
                out["status"] = res.get("status")
                deployed = res.get("recovered_order")
                out["deployed_layout"] = deployed
                if deployed is not None:
                    out["deployed_differs_from_env"] = tuple(deployed) != env_layout
                    out["deployed_in_reachable"] = tuple(deployed) in layouts if exact else None

    print(json.dumps(out))


if __name__ == "__main__":
    main()
