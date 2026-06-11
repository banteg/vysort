# vysort

Make vyper 0.3.4–0.3.7 bytecode deterministic.

These compiler versions emit nondeterministic bytecode for any contract whose
call graph contains a *decision point* — a function calling ≥2 internal
functions defined later in the file ([vyper#3369](https://github.com/vyperlang/vyper/issues/3369)).
The internal-function sections get permuted per environment (and per run on
linux), which blocks byte-exact verification: the verifier's recompile may
never reproduce what the deployer's machine happened to emit.

vysort fixes this with no compiler modifications: it decodes the deployed
layout straight off the on-chain bytecode, then reorders the source so
internal function defs come first, in that exact order. The topsort then has
zero decision points and a stock compiler produces the deployed bytecode
everywhere, every run.

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

## Verify your contract

If your vyper 0.3.x contract fails verification, this is the command:

```sh
vysort verify contract.vy --address 0x2cced4ff... --rpc-url https://eth.example
```

It fetches the deployed code and chain id from the RPC, recovers the deployed
internal-function layout from the on-chain bytes, rewrites the source to force
that layout, confirms the exact standard-json payload reproduces the runtime
byte-for-byte (a preflight compile through vyper's own std-json entry point —
the same path the verifier's binary takes), and submits it to sourcify's v2
API with a stock compiler version. No forks, no patched binaries, no special
verifier support.

Use `--dry-run` to inspect the submission payload without sending it,
`--creation-tx` to help the creation match, `--sourcify-url` to target
another server, and `-o` to keep the rewritten source.

Note: creation matches are only guaranteed when `__init__` calls ≤1 internal
function; the init-callee section of creation code is not forced by source
order. Runtime matches are always forceable.

## Match without submitting

To recover the layout and prove the match locally — against on-chain code or
a hex file — without involving a verifier:

```sh
vysort match contract.vy --address 0x2cced4ff... --rpc-url https://eth.example -o matched.vy
vysort match contract.vy --runtime runtime.hex -o matched.vy
```

The deployed layout is recovered in 2 compiles regardless of contract size:
one instrumented compile maps each internal function's section boundaries and
masks the layout-dependent address bytes, the deployed order is then decoded
straight off the on-chain bytes, and one reordered stock compile verifies it
byte-exactly — `exact`, or `prefix` when the deployed code carries an appended
immutable tail. If the decode hits an edge case, reachable layouts are
brute-forced one compile at a time as a fallback. For unaffected compiler
versions a single compile-and-compare runs instead. `--evm-version istanbul`
helps pre-berlin deployments whose nonreentrant lock constants differ.

The matched source written by `-o` is ordinary vyper that any stock compiler
of that version turns into the deployed bytecode — auxdata contains no source
hash, so the output is byte-identical to what the original source produces
under that ordering.

## Developer curiosities

The remaining subcommands expose the machinery.

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
at risk; this covers 93% of affected-band mainnet contracts. Otherwise
`layouts` (when small) enumerates every layout the deployer's heap could have
produced. The check is version-aware: sources targeting compilers outside the
affected 0.3.4–0.3.7 band short-circuit to `immune: true` without compiling.

Force an arbitrary layout by rewriting the source:

```sh
vysort reorder contract.vy _double,_triple -o forced.vy
vysort reorder contract.vy layout.json > forced.vy
```

The layout is a comma-separated list of internal function names or a JSON
file (`["_double", "_triple"]`). This is the forcing primitive `match` and
`verify` are built on.
