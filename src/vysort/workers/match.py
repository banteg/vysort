"""Brute-force the deployed internal-function layout: enumerate every layout
reachable by the 0.3.4-0.3.7 call-graph topsort, reorder the source per
candidate, stock-compile, and compare against the target runtime bytecode.

A `prefix` status means the compiled runtime is a prefix of the target and
the remainder is the appended immutable tail. In `single` mode (unaffected
compiler versions: exactly one layout is reachable) the original source is
compiled once and compared.

Self-contained worker: runs in an ephemeral `uv run --with vyper==X` env,
importing siblings from its own directory. argv: <source.vy>
<runtime-hex-file> <mode: brute|single> [evm-version]; JSON on stdout,
progress on stderr.
"""

import json
import pathlib
import sys

import vyper
from check import capture_call_graph, enumerate_layouts, simulate
from recover import recover_layout
from reorder import reorder

EVM_VERSION = sys.argv[4] if len(sys.argv) > 4 else None


def compile_runtime(src: str) -> bytes:
    kwargs = {"evm_version": EVM_VERSION} if EVM_VERSION else {}
    out = vyper.compile_code(src, output_formats=["bytecode_runtime"], **kwargs)
    return bytes.fromhex(out["bytecode_runtime"].removeprefix("0x"))


def compare(target: bytes, runtime: bytes):
    if runtime == target:
        return {"status": "exact"}
    if len(target) > len(runtime) and target.startswith(runtime):
        return {"status": "prefix", "immutable_tail_bytes": len(target) - len(runtime)}
    return None


def match_single(src: str, target: bytes) -> dict:
    verdict = compare(target, compile_runtime(src)) or {"status": "no_match"}
    out = {**verdict, "attempts": 1, "reachable_layouts": 1}
    if verdict["status"] != "no_match":
        out["source"] = src
    return out


def match_brute(src: str, target: bytes) -> dict:
    # layout recovery first: decode the deployed order straight off the
    # bytecode, then one reordered stock compile to verify — 2 compiles
    # total regardless of how many layouts are reachable
    # a decode crash must not kill the worker — brute force still rescues
    # 25 of the 26 at-risk mainnet runtimes if recovery hits an unknown
    # edge case (the decode found new ones in both validation rounds)
    try:
        rec = recover_layout(src, target, EVM_VERSION)
    except Exception as e:
        rec = {"status": f"recover_error: {type(e).__name__}: {e}"}
    if rec["status"] == "unsupported_minimal_proxy":
        return rec
    if rec["status"] == "decoded":
        reordered = reorder(src, rec["proof_order"])
        verdict = compare(target, compile_runtime(reordered))
        if verdict:
            return {
                **verdict,
                "method": "recovery",
                "layout": rec["proof_order"],
                "compiles": 2,
                "source": reordered,
            }
        print("recovered order did not verify, falling back to brute force", file=sys.stderr)
    else:
        print(f"layout recovery unavailable ({rec['status']}), falling back to brute force", file=sys.stderr)

    captured = capture_call_graph(src)
    source_order = captured.pop("__source_order__")
    internal = {n: v["internal"] for n, v in captured.items()}
    base_callees = {n: v["callees"] for n, v in captured.items()}
    env_layout = simulate(source_order, base_callees, internal)
    layouts, exact, space = enumerate_layouts(source_order, base_callees, internal)
    if not exact:
        return {
            "status": "layout_space_too_large",
            "choice_space": space,
            "sampled_layouts": len(layouts),
        }
    # the environment's own layout is the most likely match, try it first
    candidates = sorted(layouts, key=lambda la: (la != env_layout, la))
    for i, layout in enumerate(candidates, 1):
        print(f"candidate {i}/{len(candidates)}: {list(layout)}", file=sys.stderr)
        reordered = reorder(src, list(layout))
        verdict = compare(target, compile_runtime(reordered))
        if verdict:
            return {
                **verdict,
                "method": "brute",
                "layout": list(layout),
                "attempts": i,
                "reachable_layouts": len(candidates),
                "source": reordered,
            }
    return {
        "status": "no_match",
        "method": "brute",
        "attempts": len(candidates),
        "reachable_layouts": len(candidates),
    }


if __name__ == "__main__":
    src = pathlib.Path(sys.argv[1]).read_text()
    hex_str = "".join(pathlib.Path(sys.argv[2]).read_text().split()).removeprefix("0x")
    target = bytes.fromhex(hex_str)
    mode = sys.argv[3]
    out = match_single(src, target) if mode == "single" else match_brute(src, target)
    print(json.dumps(out, indent=2))
