"""Run with uv run --no-project --python 3.10 --with vyper==0.3.7 python -m unittest discover -s tests."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from vyper.compiler import phases

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/vysort/workers"))
from check import check
from match import compile_runtime, match_brute

NAMES = [f"_f{i}" for i in reversed(range(8))]
HELPERS = [
    f"@internal\n@pure\ndef {name}(x: uint256) -> uint256:\n    return x + 1\n"
    for name in NAMES
]
SUM = " + ".join(f"self.{name}(x)" for name in reversed(NAMES))
CALLER = f"@external\n@pure\ndef run(x: uint256) -> uint256:\n    return {SUM}\n"


class IdenticalFunctionsTest(unittest.TestCase):
    def test_only_reachable_layout(self):
        tag = "tag: immutable(bytes32)\n\n@external\ndef __init__():\n    tag = empty(bytes32)\n"
        source = "\n".join([tag, *HELPERS, CALLER])
        report = check(source)
        self.assertEqual(report["decision_points"], 0)
        self.assertEqual(report["choice_space"], 40320)
        self.assertEqual(report["layouts"], [tuple(NAMES)])
        self.assertTrue(report["reachable_exact"])
        self.assertTrue(report["immune"])

        runtime = compile_runtime(source)
        for tail, status in [(b"", "exact"), (bytes(32), "prefix")]:
            with self.subTest(status=status):
                with patch.object(
                    phases, "generate_ir_nodes", wraps=phases.generate_ir_nodes
                ) as compile_ir:
                    result = match_brute(source, runtime + tail)
                self.assertEqual(compile_ir.call_count, 1)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["source"], source)
                self.assertEqual(result["layout"], NAMES)

    def test_recover_entries_from_jump_operands(self):
        aggregator = f"@internal\n@pure\ndef _sum(x: uint256) -> uint256:\n    return {SUM}\n"
        for callers, layout in [
            ([CALLER], NAMES),
            ([aggregator, CALLER.replace(SUM, "self._sum(x)")], [*NAMES, "_sum"]),
        ]:
            with self.subTest(layout=layout):
                # Reverse-named helpers have identical bodies. Once the caller
                # comes first, its original source has 8! possible DFS orders.
                target = compile_runtime("\n".join([*HELPERS, *callers]))
                source = "\n".join([*reversed(callers), *HELPERS])
                result = match_brute(source, target)
                self.assertEqual(result["status"], "exact")
                self.assertEqual(result["method"], "recovery")
                self.assertEqual(result["compiles"], 2)
                self.assertEqual(result["layout"], layout)
                self.assertEqual(compile_runtime(result["source"]), target)
