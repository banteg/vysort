"""vysort: make vyper 0.3.4-0.3.7 bytecode deterministic by forcing the
internal-function layout through source definition reordering.

The CLI runs on any modern python; vyper-touching work is delegated to
self-contained worker scripts executed via `uv run` in an ephemeral
environment with the matching python and vyper versions.
"""

import argparse
import json
import pathlib
import re
import subprocess
import sys

from uv import find_uv_bin

WORKERS = pathlib.Path(__file__).parent / "workers"

# the whole affected band (vyper 0.3.4-0.3.7) supports python 3.10
DEFAULT_PYTHON = "3.10"


def detect_vyper_version(src: str) -> str:
    m = re.search(r"^#\s*(?:@version|pragma\s+version)\s+[\^~=<>!]*\s*([\d.]+)", src, re.M)
    if not m:
        raise SystemExit("no version pragma in source; pass --vyper")
    return m.group(1)


def run_worker(worker: str, args, vyper_version: str, python: str) -> str:
    cmd = [
        find_uv_bin(),
        "run",
        "--quiet",
        "--no-project",
        "--python", python,
        "--with", f"vyper=={vyper_version}",
        str(WORKERS / f"{worker}.py"),
        *args,
    ]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    if r.returncode:
        sys.exit(r.returncode)
    return r.stdout


def parse_layout(value: str) -> list:
    """Layout is a JSON file path or an inline comma-separated list of names."""
    path = pathlib.Path(value)
    if path.exists():
        return json.loads(path.read_text())
    return [n.strip() for n in value.split(",") if n.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(prog="vysort", description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("source", type=pathlib.Path, help="vyper source file")
    common.add_argument("--vyper", help="vyper version (default: from source pragma)")
    common.add_argument(
        "--python", default=DEFAULT_PYTHON, help=f"python for the compiler env (default: {DEFAULT_PYTHON})"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "check",
        parents=[common],
        help="analyze a contract for ordering nondeterminism (decision points, reachable layouts)",
    )

    p_reorder = sub.add_parser(
        "reorder",
        parents=[common],
        help="rewrite source so a stock compiler deterministically produces the target layout",
    )
    p_reorder.add_argument(
        "layout",
        type=parse_layout,
        help='target internal layout: JSON file (["_f1", ...]) or comma-separated names (_f1,_f2)',
    )
    p_reorder.add_argument(
        "-o", "--output", type=pathlib.Path, help="write reordered source here (default: stdout)"
    )

    args = parser.parse_args()
    vyper_version = args.vyper or detect_vyper_version(args.source.read_text())

    if args.command == "check":
        out = run_worker("check", [args.source], vyper_version, args.python)
        sys.stdout.write(out)
    elif args.command == "reorder":
        out = run_worker("reorder", [args.source, json.dumps(args.layout)], vyper_version, args.python)
        if args.output:
            args.output.write_text(out)
        else:
            sys.stdout.write(out)
