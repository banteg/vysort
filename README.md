# vysort

Make vyper 0.3.4–0.3.7 bytecode deterministic.

These compiler versions emit nondeterministic bytecode for any contract whose
call graph contains a *decision point* — a function calling ≥2 internal
functions defined later in the file ([vyper#3369](https://github.com/vyperlang/vyper/issues/3369)).
The internal-function sections get permuted per environment (and per run on
linux), which blocks byte-exact verification.

vysort fixes this with no compiler modifications: it reorders the source so
internal function defs come first, in the target layout order. The topsort
then has zero decision points and a stock compiler produces the same bytecode
everywhere.

## Install

```sh
uv tool install vysort
```

Or run from a checkout: `uv run vysort ...`

vysort itself runs on any modern python and depends only on uv. The
vyper-touching work runs in an ephemeral `uv run` environment with the
matching compiler: the vyper version is auto-detected from the source's
version pragma (override with `--vyper`), on python 3.10 by default
(override with `--python`). No old python or vyper install needed.

## Usage

Analyze a contract for ordering nondeterminism:

```sh
vysort check contract.vy
```

```json
{
  "internal_fns": 2,
  "decision_points": 1,
  "reachable_layouts": 2,
  "immune": false,
  "env_layout": ["_triple", "_double"],
  "layouts": [["_double", "_triple"], ["_triple", "_double"]]
}
```

`immune: true` means exactly one layout is reachable — the contract was never
at risk. Otherwise `layouts` (when small) enumerates every layout the deployer
could have gotten, ready for brute-force: reorder per candidate and
stock-compile each until the bytecode matches.

The check is version-aware: sources targeting compilers outside the affected
0.3.4–0.3.7 band short-circuit to `immune: true` without compiling.

Force a layout by rewriting the source:

```sh
vysort reorder contract.vy _double,_triple -o forced.vy
vysort reorder contract.vy layout.json > forced.vy
```

The layout is a comma-separated list of internal function names or a JSON file
(`["_double", "_triple"]`). Compiling the reordered source with any stock
vyper 0.3.7 yields the same bytecode the original source produces under that
internal layout — auxdata contains no source hash, so the output is
byte-identical.

Find the deployed layout by matching against the runtime bytecode, from a hex
file or fetched on-chain:

```sh
vysort match contract.vy --runtime runtime.hex -o matched.vy
vysort match contract.vy --address 0xf939... --rpc-url https://eth.example -o matched.vy
```

Reachable layouts are compiled one by one (environment layout first) until the
runtime matches: `exact`, or `prefix` when the deployed code carries an
appended immutable tail. For unaffected compiler versions a single
compile-and-compare runs instead. `--evm-version istanbul` helps pre-berlin
deployments whose nonreentrant lock constants differ.

Verify on sourcify — fetches the code, brute-forces the layout, and submits
the matched source through the standard flow with a stock compiler:

```sh
vysort verify contract.vy --address 0xf939... --rpc-url https://eth.example
```

The chain id comes from the RPC. Use `--dry-run` to inspect the submission
payload, `--creation-tx` to help the creation match, and `--sourcify-url` to
target another server. Note: creation matches are only guaranteed when
`__init__` calls ≤1 internal function; the init-callee section of creation
code is not forced by source order.
