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
