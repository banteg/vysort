"""Compile the exact standard-json payload that will be submitted to the
verifier — through vyper's own std-json entry point, the same path the
official binary takes — and compare the produced deployedBytecode against
the target runtime. Catches anything that diverges between the in-process
match verification and the submitted payload: settings serialization,
std-json defaults, identifier resolution.

Self-contained worker: runs in an ephemeral `uv run --with vyper==X` env.
argv: <stdjson-file> <runtime-hex-file> <contract-identifier>; JSON on
stdout.
"""

import json
import pathlib
import sys

from vyper.cli.vyper_json import compile_json, exc_handler_to_dict


def preflight(input_json: dict, target: bytes, identifier: str) -> dict:
    filename, name = identifier.rsplit(":", 1)
    output = compile_json(input_json, exc_handler=exc_handler_to_dict)
    errors = [e for e in output.get("errors", []) if e.get("severity") == "error"]
    if errors:
        return {"status": "compile_error", "errors": [e.get("message") for e in errors]}
    evm = output["contracts"][filename][name]["evm"]
    runtime = bytes.fromhex(evm["deployedBytecode"]["object"].removeprefix("0x"))
    if runtime == target:
        return {"status": "exact"}
    if len(target) > len(runtime) and target.startswith(runtime):
        return {"status": "prefix", "immutable_tail_bytes": len(target) - len(runtime)}
    n = next(
        (i for i, (x, y) in enumerate(zip(target, runtime)) if x != y),
        min(len(target), len(runtime)),
    )
    return {"status": "no_match", "common_prefix_bytes": n, "compiled_bytes": len(runtime)}


if __name__ == "__main__":
    input_json = json.loads(pathlib.Path(sys.argv[1]).read_text())
    hex_str = "".join(pathlib.Path(sys.argv[2]).read_text().split()).removeprefix("0x")
    print(json.dumps(preflight(input_json, bytes.fromhex(hex_str), sys.argv[3]), indent=2))
