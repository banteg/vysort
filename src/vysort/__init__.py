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
import tempfile
import time
import urllib.error
import urllib.request

from uv import find_uv_bin

WORKERS = pathlib.Path(__file__).parent / "workers"

# the whole affected band (vyper 0.3.4-0.3.7) supports python 3.10
DEFAULT_PYTHON = "3.10"

# call-graph ordering nondeterminism: introduced in 0.3.4, fixed in 0.3.8
AFFECTED_MIN, AFFECTED_MAX = (0, 3, 4), (0, 3, 7)

DEFAULT_SOURCIFY = "https://sourcify.dev/server"


def version_tuple(version: str) -> tuple:
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    if not m:
        raise SystemExit(f"cannot parse vyper version: {version!r}")
    return tuple(int(x) for x in m.groups())


def is_affected(version: str) -> bool:
    return AFFECTED_MIN <= version_tuple(version) <= AFFECTED_MAX


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
        *map(str, args),
    ]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    if r.returncode:
        sys.exit(r.returncode)
    return r.stdout


def http_json(url: str, payload: dict | None = None, method: str = "POST") -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"Content-Type": "application/json", "User-Agent": "vysort"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"http {e.code} from {url}: {e.read().decode(errors='replace')}")


def eth_rpc(rpc_url: str, method: str, params: list):
    reply = http_json(rpc_url, {"jsonrpc": "2.0", "method": method, "params": params, "id": 1})
    if "error" in reply:
        raise SystemExit(f"rpc error from {method}: {reply['error']}")
    return reply["result"]


def fetch_runtime(address: str, rpc_url: str) -> str:
    code = eth_rpc(rpc_url, "eth_getCode", [address, "latest"])
    if code in (None, "0x"):
        raise SystemExit(f"no code at {address}")
    return code


def resolve_runtime(args, parser) -> str:
    if args.runtime:
        return args.runtime.read_text()
    if args.address and args.rpc_url:
        return fetch_runtime(args.address, args.rpc_url)
    parser.error("supply --runtime or --address with --rpc-url")


def run_match(
    source: pathlib.Path, runtime_hex: str, vyper_version: str, python: str, evm_version: str | None
) -> dict:
    mode = "brute" if is_affected(vyper_version) else "single"
    clean = "".join(runtime_hex.split()).removeprefix("0x")
    with tempfile.NamedTemporaryFile("w", suffix=".hex") as f:
        f.write(clean)
        f.flush()
        worker_args = [source, f.name, mode] + ([evm_version] if evm_version else [])
        out = run_worker("match", worker_args, vyper_version, python)
    return json.loads(out)


def run_preflight(payload: dict, runtime_hex: str, vyper_version: str, python: str) -> dict:
    """Compile the exact submission payload via vyper's std-json path and
    compare against the deployed runtime."""
    clean = "".join(runtime_hex.split()).removeprefix("0x")
    with tempfile.NamedTemporaryFile("w", suffix=".json") as fj, tempfile.NamedTemporaryFile(
        "w", suffix=".hex"
    ) as fh:
        json.dump(payload["stdJsonInput"], fj)
        fj.flush()
        fh.write(clean)
        fh.flush()
        out = run_worker(
            "preflight", [fj.name, fh.name, payload["contractIdentifier"]], vyper_version, python
        )
    return json.loads(out)


def compiler_version_string(vyper_version: str, python: str) -> str:
    """Full version with commit hash as sourcify expects it, e.g. 0.3.7+commit.6020b8bb.

    The canonical string comes from the official release binary names (pip vyper
    reports a shorter git hash than the release assets carry)."""
    try:
        release = http_json(
            f"https://api.github.com/repos/vyperlang/vyper/releases/tags/v{vyper_version}",
            method="GET",
        )
        for asset in release["assets"]:
            m = re.match(rf"vyper\.({re.escape(vyper_version)}\+commit\.[0-9a-f]+)\.", asset["name"])
            if m:
                return m.group(1)
    except SystemExit:
        pass
    cmd = [
        find_uv_bin(),
        "run",
        "--quiet",
        "--no-project",
        "--python", python,
        "--with", f"vyper=={vyper_version}",
        "vyper", "--version",
    ]
    return subprocess.run(cmd, stdout=subprocess.PIPE, text=True, check=True).stdout.strip()


def build_std_json(filename: str, source: str, evm_version: str | None) -> dict:
    settings = {"outputSelection": {"*": ["evm.bytecode", "evm.deployedBytecode", "abi"]}}
    if evm_version:
        settings["evmVersion"] = evm_version
    return {"language": "Vyper", "sources": {filename: {"content": source}}, "settings": settings}


def sourcify_verify(sourcify_url: str, chain_id: int, address: str, payload: dict) -> dict:
    submit = http_json(f"{sourcify_url}/v2/verify/{chain_id}/{address}", payload)
    vid = submit["verificationId"]
    print(f"submitted verification job {vid}", file=sys.stderr)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        job = http_json(f"{sourcify_url}/v2/verify/{vid}", method="GET")
        if job.get("isJobCompleted"):
            return job
        time.sleep(2)
    raise SystemExit(f"verification job {vid} did not complete within 10 minutes")


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
    onchain = argparse.ArgumentParser(add_help=False)
    onchain.add_argument("--runtime", type=pathlib.Path, help="file with the target runtime bytecode hex")
    onchain.add_argument("--address", help="contract address to fetch the runtime code from")
    onchain.add_argument("--rpc-url", help="json-rpc endpoint for --address")

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

    p_match = sub.add_parser(
        "match",
        parents=[common, onchain],
        help="find the deployed layout: compile reachable layouts until the runtime matches",
    )
    p_match.add_argument("--evm-version", help="evm version for compilation (e.g. istanbul)")
    p_match.add_argument(
        "-o", "--output", type=pathlib.Path, help="write the matched reordered source here"
    )

    p_verify = sub.add_parser(
        "verify",
        parents=[common],
        help="match the deployed layout and submit the source to sourcify",
    )
    p_verify.add_argument("--address", required=True, help="deployed contract address")
    p_verify.add_argument("--rpc-url", required=True, help="json-rpc endpoint (also provides the chain id)")
    p_verify.add_argument("--sourcify-url", default=DEFAULT_SOURCIFY, help=f"sourcify server (default: {DEFAULT_SOURCIFY})")
    p_verify.add_argument("--creation-tx", help="contract creation tx hash (helps the creation match)")
    p_verify.add_argument("--evm-version", help="evm version for compilation (e.g. istanbul)")
    p_verify.add_argument(
        "--dry-run", action="store_true", help="print the submission payload instead of submitting"
    )
    p_verify.add_argument(
        "-o", "--output", type=pathlib.Path, help="write the matched reordered source here"
    )

    args = parser.parse_args()
    vyper_version = args.vyper or detect_vyper_version(args.source.read_text())

    if args.command == "check":
        if not is_affected(vyper_version):
            report = {
                "vyper": vyper_version,
                "affected": False,
                "immune": True,
                "reason": "call-graph ordering bug only affects vyper 0.3.4-0.3.7",
            }
        else:
            report = {"vyper": vyper_version, "affected": True}
            report.update(json.loads(run_worker("check", [args.source], vyper_version, args.python)))
        print(json.dumps(report, indent=2))

    elif args.command == "reorder":
        out = run_worker("reorder", [args.source, json.dumps(args.layout)], vyper_version, args.python)
        if args.output:
            args.output.write_text(out)
        else:
            sys.stdout.write(out)

    elif args.command == "match":
        runtime_hex = resolve_runtime(args, parser)
        result = run_match(args.source, runtime_hex, vyper_version, args.python, args.evm_version)
        source = result.pop("source", None)
        if args.output and source:
            args.output.write_text(source)
            result["output"] = str(args.output)
        print(json.dumps({"vyper": vyper_version, "affected": is_affected(vyper_version), **result}, indent=2))
        if result["status"] not in ("exact", "prefix"):
            sys.exit(1)

    elif args.command == "verify":
        runtime_hex = fetch_runtime(args.address, args.rpc_url)
        chain_id = int(eth_rpc(args.rpc_url, "eth_chainId", []), 16)
        result = run_match(args.source, runtime_hex, vyper_version, args.python, args.evm_version)
        source = result.pop("source", None)
        if result["status"] not in ("exact", "prefix"):
            print(json.dumps(result, indent=2))
            raise SystemExit(f"runtime matches no reachable layout (status: {result['status']}); not submitting")
        if args.output:
            args.output.write_text(source)
        filename = args.source.name
        payload = {
            "stdJsonInput": build_std_json(filename, source, args.evm_version),
            "compilerVersion": compiler_version_string(vyper_version, args.python),
            "contractIdentifier": f"{filename}:{args.source.stem}",
        }
        if args.creation_tx:
            payload["creationTransactionHash"] = args.creation_tx
        preflight = run_preflight(payload, runtime_hex, vyper_version, args.python)
        if preflight["status"] not in ("exact", "prefix"):
            print(json.dumps({"match": result, "preflight": preflight}, indent=2))
            raise SystemExit(
                f"preflight std-json compile does not reproduce the runtime "
                f"(status: {preflight['status']}); not submitting"
            )
        print(f"preflight: std-json payload reproduces the runtime ({preflight['status']})", file=sys.stderr)
        if args.dry_run:
            print(json.dumps(
                {
                    "chain_id": chain_id,
                    "address": args.address,
                    "match": result,
                    "preflight": preflight,
                    "payload": payload,
                },
                indent=2,
            ))
            return
        job = sourcify_verify(args.sourcify_url, chain_id, args.address, payload)
        report = {
            "match": result["status"],
            "preflight": preflight["status"],
            "layout": result.get("layout"),
            "contract": job.get("contract"),
            "error": job.get("error"),
        }
        print(json.dumps(report, indent=2))
        if not (job.get("contract") or {}).get("match"):
            sys.exit(1)
