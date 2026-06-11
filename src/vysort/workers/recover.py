"""Recover the deployed internal-function layout from on-chain runtime
bytecode in 1 instrumented compile plus an offline decode (no search):

  1. instrumented compile: walk the runtime assembly to get each internal
     function's section boundaries (entry-label pcs) and all code-symbol
     PUSH2 operand positions, masking only operands whose target lies in
     the movable internal region;
  2. decode the deployed section order by masked comparison (no compiles).

Section lengths are order-invariant, so the decode terminates with the full
deployed permutation regardless of how many layouts are reachable.

Handles: shared revert/postamble after the last internal function (excluded
from the movable region), cleanup-only internals (no emitted main label —
anchored to the host section containing their cleanup pc and re-inserted
into the proof order), and EIP-1167 minimal proxies (classified, not
decoded).

Self-contained worker module: runs in an ephemeral `uv run --with vyper==X`
env; used by match.py, not invoked directly.
"""

import re

import vyper.evm.opcodes as evm_ops
from vyper.compiler.phases import CompilerData
from vyper.ir.compile_ir import is_mem_sym, is_ofst, is_symbol

EIP1167_RE = re.compile(r"^363d3d373d3d3d363d73([0-9a-f]{40})5af43d82803e903d91602b57fd5bf3$")


def scan(assembly):
    """Replicate assembly_to_evm's pc walk.

    Returns (labels: symbol -> pc, refs: [(operand_pc, symbol)], code_end).
    """
    pc, labels, refs = 0, {}, []
    for i, item in enumerate(assembly):
        if item == "DEBUG":
            continue
        if is_symbol(item):
            if assembly[i + 1] in ("JUMPDEST", "BLANK"):
                labels[item] = pc
            else:
                refs.append((pc + 1, item))
                pc += 3
        elif is_mem_sym(item):
            raise AssertionError("mem symbol in runtime assembly")
        elif is_ofst(item):
            pc -= 1  # matches upstream: following symbol overlaps by one byte
        elif item == "BLANK" or (isinstance(item, str) and item.startswith("_DEPLOY_MEM_OFST_")):
            pass
        elif isinstance(item, list):
            raise AssertionError("subcode in runtime assembly")
        else:
            pc += 1
    return labels, refs, pc


def recover_layout(src: str, onchain: bytes, evm_version=None) -> dict:
    """One instrumented compile + offline decode of the deployed order.

    Returns {"status": "decoded", "proof_order": [...], ...} on success;
    other statuses: unsupported_minimal_proxy, too_few_sections,
    initcode_passed, decode_failed."""
    m = EIP1167_RE.match(onchain.hex())
    if m:
        return {"status": "unsupported_minimal_proxy", "implementation": "0x" + m.group(1)}

    if evm_version:
        # 0.3.x anchors the evm version in a module global (no kwarg on CompilerData)
        evm_ops.active_evm_version = evm_ops.EVM_VERSIONS[evm_version]
    data = CompilerData(src)
    bc = data.bytecode_runtime
    internal_sigs = {n: s for n, s in data.function_signatures.items() if s.internal}
    labels, refs, code_end = scan(data.assembly_runtime)

    starts, missing = {}, []
    for name, sig in internal_sigs.items():
        main_pc = labels.get("_sym_" + sig.internal_function_label)
        if main_pc is not None:
            starts[name] = main_pc
        else:
            missing.append((name, sig))

    if len(starts) < 2:
        # nothing movable (cleanup-only internals can't reorder on their own)
        return {"status": "too_few_sections", "internal_sections": len(starts)}

    order = sorted(starts, key=starts.get)
    movable_start = starts[order[0]]
    # the shared revert/postamble after the last function is NOT movable:
    # bound the last section at the first _sym_revert* label past its start
    revert_pcs = [
        pc for s, pc in labels.items() if s.startswith("_sym_revert") and pc > starts[order[-1]]
    ]
    movable_end = min(revert_pcs) if revert_pcs else code_end
    bounds = [starts[f] for f in order] + [movable_end]
    sections = {f: (bounds[i], bounds[i + 1]) for i, f in enumerate(order)}

    # cleanup-only internals: no emitted main label; their remnant code lives
    # inside another function's section. Anchor each to that host section so
    # the proof order can re-insert it right after its host. Internals with
    # neither label are never emitted in the runtime (unreachable from any
    # external function, e.g. dead code or constructor-only) — their source
    # position cannot affect the runtime layout, so they are left out of the
    # proof order; the final byte-exact comparison validates this.
    skipped, absent = [], []
    for name, sig in missing:
        cleanup_pc = labels.get("_sym_" + sig.exit_sequence_label)
        host = None
        if cleanup_pc is not None:
            host = next((f for f, (lo, hi) in sections.items() if lo <= cleanup_pc < hi), None)
        if cleanup_pc is None:
            absent.append(name)
        elif host is None:
            return {
                "status": "decode_failed",
                "reason": f"cleanup label of {name} at pc {cleanup_pc} falls outside every internal section",
            }
        else:
            skipped.append({"function": name, "attach_after": host, "cleanup_pc": cleanup_pc})

    # mask only operands whose target label sits inside the movable region;
    # refs to fixed targets (external section, postamble, code_end) must match
    # as-is. _sym_code_end is the one symbol the assembler defines outside its
    # main loop; it resolves to code_end, which is layout-invariant.
    labels.setdefault("_sym_code_end", code_end)
    mask = set()
    for operand_pc, sym in refs:
        target = labels.get(sym)
        if target is None:
            raise AssertionError(f"reference to undefined symbol {sym}")
        if movable_start <= target < movable_end:
            mask.update((operand_pc, operand_pc + 1))

    def eq_masked(off, lo, hi):
        if off + (hi - lo) > len(onchain):
            return False
        return all(onchain[off + i - lo] == bc[i] for i in range(lo, hi) if i not in mask)

    external_ok = eq_masked(0, 0, movable_start)

    if not external_ok:
        deploy_bc = data.bytecode
        runtime_offset = deploy_bc.find(bc)
        if (
            runtime_offset >= 0
            and len(onchain) == len(deploy_bc)
            and onchain[: min(runtime_offset, 32)] == deploy_bc[: min(runtime_offset, 32)]
            and eq_masked(runtime_offset, 0, movable_start)
        ):
            return {
                "status": "initcode_passed",
                "reason": "target looks like Vyper creation bytecode, but layout recovery expects deployed runtime bytecode",
                "runtime_offset": runtime_offset,
                "runtime_bytes": len(bc),
                "hint": "pass deployed runtime bytecode from eth_getCode/cast code, or extract/simulate the creation result before matching",
            }

    cursor, deployed, remaining = movable_start, [], set(order)
    while remaining:
        for f in sorted(remaining):
            lo, hi = sections[f]
            if eq_masked(cursor, lo, hi):
                deployed.append(f)
                remaining.discard(f)
                cursor += hi - lo
                break
        else:
            return {
                "status": "decode_failed",
                "reason": f"no section matches at offset {cursor}",
                "recovered_so_far": deployed,
                "external_region_ok": external_ok,
            }

    proof_order = []
    for f in deployed:
        proof_order.append(f)
        proof_order.extend(
            s["function"]
            for s in sorted(skipped, key=lambda s: s["cleanup_pc"])
            if s["attach_after"] == f
        )

    return {
        "status": "decoded",
        "recovered_order": deployed,
        "proof_order": proof_order,
        "skipped_internals": [s["function"] for s in skipped],
        "absent_internals": absent,
        "internal_sections": len(order),
        "external_region_ok": external_ok,
    }
