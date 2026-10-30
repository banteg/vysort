"""Match the deployed internal-function layout, falling back to enumeration
if it cannot be recovered from the target runtime bytecode.

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
import re
import sys

import vyper
import vyper.evm.opcodes as evm_ops
from check import call_graph, decision_points, enumerate_layouts, simulate
from recover import recover_layout
from reorder import reorder
from vyper.compiler.phases import CompilerData

EIP1167_RE = re.compile(r"363d3d373d3d3d363d73([0-9a-f]{40})5af43d82803e903d91602b57fd5bf3")
EVM_VERSION = None


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
    proxy = EIP1167_RE.fullmatch(target.hex())
    if proxy:
        return {"status": "unsupported_minimal_proxy", "implementation": "0x" + proxy.group(1)}

    data = CompilerData(src)
    callees, internal = call_graph(data)
    env_layout = simulate(callees, internal)
    decisions = decision_points(callees, internal)
    if not decisions:
        verdict = compare(target, data.bytecode_runtime)
        if verdict:
            return {
                **verdict,
                "method": "single",
                "layout": list(env_layout),
                "reachable_layouts": 1,
                "compiles": 1,
                "source": src,
            }

    # An unexpected compiler assembly shape can still match via enumeration.
    try:
        rec = recover_layout(data, target)
    except Exception as e:  # noqa: BLE001
        rec = {"status": f"recover_error: {type(e).__name__}: {e}"}
    if rec["status"] == "initcode_passed":
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

    layouts, exact, space = enumerate_layouts(callees, internal, env_layout, decisions)
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
    EVM_VERSION = sys.argv[4] if len(sys.argv) > 4 else None
    if mode == "single":
        out = match_single(src, target)
    else:
        # CompilerData reads the active EVM version set by Vyper's wrapper.
        out = evm_ops.evm_wrapper(match_brute)(src, target, evm_version=EVM_VERSION)
    print(json.dumps(out, indent=2))
