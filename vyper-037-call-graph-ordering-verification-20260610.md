# Making Vyper 0.3.7 Verifiable: Call-Graph Ordering Nondeterminism

Generated: 2026-06-10

## Problem

Vyper 0.3.4–0.3.7 emit nondeterministic bytecode for any contract whose call
graph contains more than one internal function reachable from the same caller.
[vyperlang/vyper#3369](https://github.com/vyperlang/vyper/issues/3369),
fixed by [PR #3370](https://github.com/vyperlang/vyper/pull/3370) (shipped in
0.3.8, never backported). This blocks byte-exact verification: the verifier's
recompile can produce a different bytecode than the deployer got, with no
documented way to reproduce the deployed output.

Mainnet exposure (from `data/vyper_inventory.parquet`): 2,045 contracts
(1,093 unique runtimes) on 0.3.7, plus 101 on 0.3.6, 70 on 0.3.4, 1 on 0.3.5 —
roughly 2,200 contracts in the affected version band.

## Root cause (from vyper source at v0.3.7)

- `ContractFunction.called_functions` is a plain `set()`
  (`vyper/semantics/types/function.py:126`).
- Neither `ContractFunction` nor its bases define `__hash__`, so elements hash
  by `id()` — i.e. **memory address**. Iteration order depends on heap layout,
  not on `PYTHONHASHSEED` (string-hash randomization is irrelevant; pinning it
  does nothing, confirming the reporter's failed attempt in #3369).
- The set order leaks into output in exactly two places
  (`vyper/codegen/module.py`):
  - `_topsort_helper()` DFS uses `called_functions` iteration order →
    determines the **order of internal-function code sections** in the runtime
    bytecode (which shifts every subsequent jump target).
  - deploy code appends `__init__`-called internal functions in **raw set
    order** (line 224) → creation bytecode is permuted the same way.
- Frame allocation uses `max()` over callee frame sizes
  (`function_definitions/common.py`), so memory offsets are
  order-**independent**. The entire divergence is the permutation of internal
  function sections; everything else follows deterministically from it.

The bug was introduced by `4b44ee7` ("fix: call internal functions from
constructor", #2496, 2022-05-09), first released in **v0.3.4**; fix (OrderedSet)
first released in **v0.3.8**. Versions ≤ 0.3.3 are clean: `module.py` had no
call-graph topsort — `parse_regular_functions` emits internal functions in
plain source order.

### 0.3.4–0.3.6 in the wild (added 2026-06-11)

The same defective code ships in 0.3.4–0.3.6, but no failure has been
*observed* there:

- The sourcify submission logs in this corpus
  (`source_enrichment/…/sourcify_submissions/`, 2,223 submissions) contain
  "bytecode don't match" failures **only** for 0.3.7 (105 addresses) and
  0.4.3 (11 addresses — triaged below, unrelated).
  All other failures are unrelated: utf-8 `Compiler error` on 0.2.x (115),
  unfetchable `0.1.0bX` binaries, duplicate-NatSpec errors on 0.3.10,
  Etherscan version-mapping failures.
- **0.4.3 triage (added 2026-06-11): all 11 are ERC-5202 blueprints, zero
  compiler issues.** The 11 addresses collapse to 6 unique on-chain codes,
  every one starting with the `fe7100` blueprint preamble. Stripping the
  3-byte preamble, each body is **byte-exact** vyper 0.4.3 initcode compiled
  from the Etherscan standard-json with default settings (no constructor
  args, no venom, no evm/optimize variance — verified with a full settings
  sweep). The inventory classifies all of them correctly
  (`row_kind='blueprint'`); they were submitted as regular contracts only in
  the first two submission runs (2026-06-05 15:54/16:59), *before* the
  submitter's `blueprint_skip` guard first fires (20:32). Stale artifacts of
  the pre-guard pipeline, not a verification gap in the compiler sense —
  though note sourcify has no ERC-5202 blueprint verification mode (compare
  body against creation bytecode), which is what these would need to verify.
- 0.3.4–0.3.6 contracts were **never submitted** (absent from the logs), so I
  tested them directly: all 60 source-available mainnet contracts
  (42 unique runtimes; 36 sourcify-dataset, 6 etherscan/chainsecurity)
  compiled with their matching compiler — **40/42 exact match, 2/42
  immutable-tail-only, 0/42 prefix drift**, including 15 contracts with ≥5
  internal functions (2 needed `istanbul`/`berlin` evm_version, consistent
  with the CurveRouter finding). Classifier: `scripts/classify_034_036.py`
  (run per version: `uv run --python 3.10 --with vyper==0.3.4 python
  scripts/classify_034_036.py 0.3.4`; reads the worklist exported from
  `source_coverage.parquet` + `vyper_inventory.parquet`).

So the bug is **latent but unobserved** in 0.3.4–0.3.6: a small population
(172 mainnet contracts, ~60 with sources, mostly simple), and 36/42 tested
runtimes are selection-biased (sourcify dataset = already verified). The
114 source-less 0.3.4–0.3.6 contracts remain untested; layout recovery
applies to them identically if sources surface.

## What others do (researched)

- **Etherscan**: no public workaround exists. The issue reporter
  (Enigmatic331) is Etherscan's verification engineer; the thread ends
  unresolved with Curve noting verification "will continue to fail". Yet
  Etherscan shows Exact Match for large 0.3.7 contracts (crvUSD Controller)
  today, and also verified both prefix-drift contracts we examined below —
  most plausibly by retrying compilation; nothing is published.
- **Curve** dodged it: tricrypto-ng (the contract from the issue) was deployed
  with 0.3.9, not 0.3.7. crvUSD went out as 0.3.7 and got verified anyway.
- **Blockscout / Verifier Alliance / Sourcify**: no special handling anywhere
  (verified by code inspection). Sourcify verified the crvUSD Controller with
  `creationMatch` on a single recompile (runtime match blocked by the separate
  legacy-immutables issue).
- **Vyper upstream**: no 0.3.7.1 patch release, no official recommendation for
  verifying existing 0.3.7 deployments.

## What I tried (all on CurveTricryptoOptimizedWETH from the issue gist, 21 internal functions, unless noted)

| Experiment | Result |
|---|---|
| pip vyper==0.3.7, macOS, py3.10, 60 + 3 runs | fully deterministic within env |
| pip, macOS, py3.8 / 3.9 / 3.10 / 3.11 | **4 different bytecodes** — one per python version |
| filename-length / env-var padding perturbation | no effect (allocation offsets are per-size-class, insensitive to unrelated allocations) |
| `PYTHONHASHSEED=0` | no effect / does **not** stabilize (id-based hashing) |
| official darwin binary (PyInstaller), 16 runs | **nondeterministic per run**: 6 distinct outputs, skewed (~60% mode) |
| official **linux** binary — what sourcify runs — 6 runs (amd64 docker) | **nondeterministic per run**: 2 distinct outputs |
| pip vyper==0.3.7, linux py3.10 (docker), 3 runs | **nondeterministic per run**: 2 distinct outputs |
| docker image `vyperlang/vyper:0.3.7` | yet another ordering |
| total distinct tricrypto orderings observed | **~15 and counting** across environments |

Conclusion from the matrix: per-environment determinism is unreliable (linux —
the environment that matters for both deployers and sourcify — flakes per
run), and the reachable ordering space is far too large for an
"environment matrix" or blind-retry strategy on big contracts.

### End-to-end validation on real unverifiable mainnet contracts

The corpus check from 2026-06-09
(`reports/legacy-vyper-immutable-prefix-drift-20260609.csv`) left 6
prefix-drift contracts (2 unique runtimes) that no straightforward recompile
reproduces:

1. **Yearn Vault Factory** (`0x1d47ccdd…` + 4 more deployments,
   runtime `0xbfbd5c0b…`, 4 internal functions). No environment in the matrix
   above reproduces it. I monkeypatched `ContractFunction.called_functions`
   with a priority-ordered set (running vyper 0.3.7 as a library) and
   enumerated all 4! = 24 orderings: **found the deployer's exact ordering**
   (`_pack_protocol_fee_data, _unpack_fee_recipient, _unpack_protocol_fee,
   _unpack_custom_flag`) → byte-exact prefix match with a 32-byte immutable
   tail (handled by the in-flight legacy-immutables support). Search took
   seconds.
2. **CurveRouter v1.0** (`0x9f2fa770…`, runtime `0xdfc206a2…`). Divergence was
   environment-independent at byte 449 — *not* the ordering bug. The deployed
   code uses nonreentrant lock values `0/1`; vyper 0.3.7 emits `3/2` for EVM ≥
   berlin (`get_nonreentrant_lock`, `function_definitions/utils.py`). Compiling
   with `--evm-version istanbul` → byte-exact prefix match + 96-byte immutable
   tail. **Settings issue, not a compiler bug** — the corpus pipeline should
   sweep pre-berlin evm versions for 0.3.x.

With these two results plus the 99 tail-only rows, **all 105 observed 0.3.7
sourcify mismatches in the corpus are explained and recoverable**.

### Scaling beyond k!: greedy prefix search (failed), layout recovery (works)

k! enumeration is fine for k ≤ ~6 but not for tricrypto-class contracts
(k = 21). Two approaches tried:

**Greedy prefix-guided search — negative result.** Fix the order position by
position, keeping the candidate that maximizes the common bytecode prefix with
the target. Failed on tricrypto (232 compiles, prefix stuck at 635/23701
bytes): the external/selector section contains PUSH2 references to *every*
internal function's address, so the common prefix saturates at the first
reference to any misplaced function and carries no positional signal. Also,
a global priority fed into the DFS does not map 1:1 onto the final layout.

**Layout recovery — the actual solution.** Don't search; *read the deployed
order off the on-chain bytecode*. Two iterations:

- v1 (`layout_recover.py`, 8 compiles): compile ~7 probes with different
  seeded call-graph orderings; section boundaries from the source map; bytes
  that vary across probes are the embedded address bytes → per-function mask.
- **v2 (`layout_recover2.py`, 2 compiles)** — since the compiler runs as a
  library, instrument it directly instead of diffing probes:
  1. **One instrumented compile.** Replicate `assembly_to_evm`'s pc walk over
     `CompilerData.assembly_runtime` to extract (a) each internal function's
     entry-label pc (`_sym_` + `internal_function_label`) → exact section
     boundaries, and (b) the byte positions of every code-symbol PUSH2
     operand → the *complete* set of layout-dependent bytes, by construction
     (no under-masking possible, unlike probe diffing).
  2. **Decode (no compiles).** Walk the on-chain internal region; at each
     cursor the unique remaining function whose masked section matches is the
     next one. Section lengths are order-invariant, so this terminates with
     the full deployed permutation.
  3. **One verification compile** with the recovered order forced into
     `_topsort` → byte-exact comparison.

Results (v2):

- **tricrypto (k = 21)**: target = a random official-binary output (an
  ordering never produced by any tested environment). Deployed order decoded
  and reproduced **byte-exact (incl. CBOR auxdata) in 7.6 s, 2 compiles**.
- Robustness sweep: 6 tricrypto targets from 5 distinct orderings (official
  binary ×2 runs, pip py3.9, pip py3.11, docker image) → **6/6 exact match**.
- **Yearn Vault Factory (real mainnet target)**: deployed order decoded and
  byte-exact prefix match + 32-byte immutable tail in **0.7 s**.

Complexity is 2 compiles regardless of k — strictly better than the
k(k+1)/2 search in every dimension, and it cannot get stuck in local optima.

### Hardening (2026-06-11): 85-contract corpus validation

A run over an 85-contract sanitized mainnet corpus
(`/private/tmp/vyper037-layout-corpus/`) exposed four real-world failure
modes in the first cut of `layout_recover2.py`, all fixed:

1. **Shared revert/postamble**: the last internal section was bound to
   `code_end`, accidentally including the shared revert postamble in whichever
   function happened to be last in the analysis layout (broke case `0070`,
   where `_verify_proof` was last in analysis but earlier in deployed).
   Fixed: the movable region ends at the first `_sym_revert*` label after the
   last internal main label.
2. **Cleanup-only internals**: some internals emit no main label, only an
   `…_cleanup` label (`_get_gauge_type` in cases `0042`/`0064`); their remnant
   code rides inside another function's section. They were dropped from the
   proof order, so the verification compile no-matched at byte 22. Fixed:
   anchored to the host section containing the cleanup pc and re-inserted
   into the proof order right after the host.
3. **Over-masking**: all code-symbol operands were masked, including refs to
   position-fixed targets (external section, postamble). Fixed: refs are kept
   as `(operand_pc, symbol)` and only operands whose target pc lies inside
   the movable region are masked — refs to fixed targets must now match
   exactly, tightening the decode.
4. **EIP-1167 minimal proxies** were reported as decode failures. Fixed:
   classified up front (`unsupported_minimal_proxy`, with the implementation
   address extracted).

Plus batch ergonomics: a final `RESULT {json}` line (status, recovered/proof
order, skipped internals) and meaningful exit codes (0 exact/prefix,
2 unsupported proxy, 1 anything else).

Corpus result after fixes: **51 exact + 31 prefix (immutable tail) + 3
minimal proxies = 85/85 accounted, zero decode failures, zero no-matches.**

### Decision-point theory (2026-06-11): when the bug can manifest at all

Hypothesis (banteg): nondeterminism requires *multiple valid topsorts*.
Confirmed and sharpened — the precise condition is a **decision point**: a
`called_functions` set containing ≥2 internal functions *not yet placed* when
the DFS reaches it. Because `_topsort_helper`'s outer loop walks all function
defs in source order, any internal defined before its caller is placed
deterministically by the outer loop; by the time the caller's set is
iterated, dedup makes its order irrelevant. Freedom exists only when a
function calls ≥2 internals defined later in the file and not placed via any
earlier path.

Validated with a call-graph simulator (`scripts/theory_check.py`) that
enumerates all per-set iteration orders and compares against the deployed
layouts decoded from the 85-contract corpus:

- **73/85 (86%) have zero decision points → exactly one valid topsort →
  bit-for-bit deterministic in every environment.** These contracts were
  never at risk; this — not environment luck — is why most 0.3.x contracts
  verify on the first compile.
- All 7 contracts whose deployed layout differed from the analysis layout sit
  in the 12-contract multi-layout pool. Zero counterexamples; every decoded
  deployed layout is inside the predicted reachable set.
- Function count predicts nothing: a 28-internal contract is immune, a
  3-internal one is not. The risk factor is source style — "externals first,
  internal helpers later" (the Curve house style) creates unplaced-callee
  sets.
- Reachable-layout counts collapse far below k!: Yearn factory 6 of 24
  permutations; tricrypto ≥1,986 (sampled) of a 5×10¹⁶ choice space.
- Retro-explanation: the synthetic `test_multi.vy` used in early experiments
  has zero decision points (all internals defined before the caller), i.e. it
  was accidentally immune — explaining its perfect stability everywhere.

### Vanilla-compiler forcing (2026-06-11): source reordering replaces the fork

The theory implies a forcing mechanism that needs **no compiler
modifications at all**: move the internal function defs to the front of the
source file, ordered by the target layout (`scripts/reorder_source.py`, using
vyper's AST for exact spans). Internals then get placed by the outer
source-order loop before any caller's set is consulted — zero decision
points, one reachable layout, in any environment. Since 0.3.7's auxdata
contains no source hash (only the compiler version), the output is
byte-identical to what the original source produces under that ordering.

Proven:

- **tricrypto**: reordered source → EXACT match with the stock pip compiler
  and **3/3 EXACT matches from the official PyInstaller binary** — the same
  binary that is per-run nondeterministic on the original source.
- **Yearn Vault Factory** (the real mainnet case): prefix match + immutable
  tail with stock pip and official binaries, all runs.
- The `@nonreentrant` lock-slot concern did not materialize for single-key
  contracts (tricrypto uses one `"lock"` key). Contracts with multiple
  distinct lock keys could theoretically permute slot assignment under
  reordering — untested; the final byte-exact comparison catches it if so.

This also enables **brute force without any decode step** for most at-risk
contracts: reachable layouts are statically enumerable from the call graph
(zero compiles), and the sets are small (6 for the Yearn factory) — reorder
per candidate and stock-compile each until match. Tricrypto-class outliers
(thousands of reachable layouts) still want the 2-compile layout recovery to
obtain the layout, followed by one reordered stock compile.

Net effect on integration: contracts become verifiable **today, with
unmodified compilers and an unmodified sourcify** — submit the
layout-reordered source and it verifies through the standard flow. The
trade-off is cosmetic: the verified source's function definition order
differs from the author's canonical file. The in-server layout-recovery path
(below) remains the option for verifying author-canonical sources
byte-for-byte.

### Population sweep (2026-06-11): how many contracts are actually at risk

Decision-point analysis over **every source-available 0.3.4–0.3.7 mainnet
contract** in this corpus (`scripts/sweep_dp.py`, results in
`reports/vyper-034-037-decision-point-sweep-20260611.jsonl`): 379 unique
runtimes / 772 deployments; 376 analyzed (3 source errors: 2 type-mismatch
compiles, 1 corrupted Etherscan source with a curly quote).

| version | unique runtimes | deployments | at-risk runtimes | at-risk deployments |
|---|---|---|---|---|
| 0.3.4 | 21 | 28 | 0 | 0 |
| 0.3.5 | 1 | 1 | 0 | 0 |
| 0.3.6 | 20 | 31 | 3 | 3 |
| 0.3.7 | 334 | 712 | 23 | 33 |
| **total** | **376** | **772** | **26 (7%)** | **36 (5%)** |

- **93% of unique runtimes are provably immune** (single valid topsort).
- The at-risk reachable-layout counts are tiny: 25 of 26 have **≤24 reachable
  layouts** (9× 2, 6× 4, 3× 6, 1× 8, 4× 12, 1× 16, 1× 24) — i.e.
  brute-forceable with at most 24 reorder-and-stock-compile attempts each.
  Exactly one outlier (`dcc516f0…`, 23 internal fns, ≥4,988 sampled layouts)
  needs the 2-compile layout-recovery decode.
- Cross-checked against live sourcify: of the 36 at-risk deployments, 21 are
  already verified (including the 5 Yearn factories from the production PoC
  and several that won the ordering lottery), leaving a **rescue queue of 13
  unique runtimes / 15 addresses** currently unverified.
- Caveat: this covers source-available contracts only (~334 of ~1,093 unique
  0.3.7 mainnet runtimes have sources in this corpus); the proportion should
  extrapolate, but source-less contracts can't be classified until sources
  surface.

### The outlier, recovered (2026-06-11): yETH pool, and a creation-side caveat

The one sweep outlier — `0x2cced4ff…` (yETH weighted stableswap pool,
0xkorin/Yearn, 23 internal functions, ≥4,988 reachable layouts, the only
contract where brute force is infeasible) — went through the full pipeline:

- **Layout recovery**: deployed 23-function order decoded in 2 compiles /
  32 s, prefix match + 32-byte immutable tail.
- **Stock-compiler forcing**: reordered source → byte-exact runtime prefix
  with pip vyper, *and* byte-exact creation code through the full compiled
  length (the 736 trailing bytes are constructor args).
- **lib-sourcify (PR #2817 branch)**: `runtimeMatch: partial` +
  `creationMatch: partial`, immutable (the yETH token address) and
  constructor args (token, amplification, 5 LSD assets + rate providers +
  weights) fully decoded.

**New limitation characterized along the way**: source reordering forces the
*runtime* topsort, but the `__init__`-callee section of *creation* code is
emitted by iterating the raw `called_functions` set directly
(`codegen/module.py` line 224 at v0.3.7) — no outer-loop protection, so its
order stays heap-dependent regardless of source order. Concretely: the
official PyInstaller binary compiled the reordered source with a *different*
init-callee order (creation divergence at byte 819, inside the deploy
section's references), while pip vyper produced the deployed order. So:

- runtime matches are always forceable with a stock compiler;
- creation matches are guaranteed only when `__init__` calls ≤1 internal
  function; otherwise they keep a (much smaller) environment lottery, fixable
  by compiler choice/retry — or by the instrumented path, which could decode
  and force the init-callee order the same way it does the runtime layout.

(This contract was already creation-matched on production sourcify in April —
that run won both lotteries at once — so no production action was needed;
its runtime match lands with PR #2817.)

### Production PoC (2026-06-11): both failure classes verified on real infra

- **Ordering class — Yearn Vault Factory** (non-immune: 1 decision point,
  6 reachable layouts, deployed ordering reproducible in no stock
  environment): layout-recovered, def-reordered source submitted to
  **production sourcify.dev** → `creationMatch: match` for **all 5 mainnet
  deployments** (`0x1d47ccdd…`, `0x292246…`, `0x46497b…`, `0x4a3d06…`,
  `0xdcf481…`), previously fully unverified. Stock
  `0.3.7+commit.6020b8bb` binary, zero sourcify-side changes; creation tx
  hashes resolved via a local reth `ots_getContractCreator`. `runtimeMatch`
  stays null pending legacy-immutable support (sourcify PR #2817) for the
  32-byte tail; verified locally against that branch:
  `runtimeMatch: partial` + `creationMatch: partial` with the immutable and
  constructor-args transformations fully decoded.
- **Immutables class — crvUSD** (`0xf939e0a0…`, immune: 0 decision points,
  as is the crvUSD Controller `0x8472a9a7…` with 16 internals — only
  tricrypto among the flagship Curve 0.3.7 contracts is non-immune):
  verified locally against sourcify PR #2817 →
  `runtimeMatch: partial, creationMatch: partial`, immutable tail decoding
  to "Curve.Fi USD Stablecoin"/"crvUSD"/EIP-712 domain separator. PR #2817
  alone unblocks it (and the 99 tail-only corpus contracts) once deployed;
  a re-verification pass is needed afterwards to pick up runtime matches.

## Chosen direction

**Layout-recovery verification for vyper 0.3.4–0.3.7**: when a contract in the
affected version band fails to match with the prefix-drift signature, recover
the deployed internal-function order from the on-chain bytecode via masked
probe compiles, then recompile once with that order forced.

Concretely, in order of increasing effort:

1. **Quick win (no compiler changes): bounded retry.** Sourcify's stock linux
   binary is per-run nondeterministic, so today even *currently-verifiable*
   0.3.7 contracts occasionally fail (false negatives), and a handful of
   retries also recovers contracts whose deployer ordering is in the
   high-probability set. Retry compilation up to N times (N ≈ 5–10) for
   0.3.4–0.3.7 when the mismatch has the prefix-drift signature. Cheap,
   fits `Verification.verify()` in lib-sourcify
   (`packages/lib-sourcify/src/Verification/Verification.ts`), no new infra.
   Does not guarantee a match for arbitrary deployer orderings.

2. **Complete fix: patched 0.3.7 build + layout recovery.** Maintain a tiny
   fork of v0.3.7 (a few dozen lines, prototype already working as a
   monkeypatch) that (a) backports the `OrderedSet` fix from PR #3370 so
   default output is deterministic, (b) accepts an explicit internal-function
   layout override (env var or an extra standard-json settings field, e.g.
   `settings.internalFunctionOrder: ["a","b",…]`), and (c) optionally exposes
   the section/mask info from the instrumented assembly walk. The verifier
   runs layout recovery — **2 compiles total**: one instrumented compile to
   get section boundaries + address masks, an offline decode of the deployed
   order, and one verification compile — and, on success, records the
   recovered order alongside the compilation metadata, making the verified
   result **reproducible by anyone** with the patched binary and the recorded
   order. This makes *every* 0.3.4–0.3.7 contract verifiable in principle,
   for runtime *and* creation matches (the same order drives the runtime
   topsort; the `__init__` callee section in creation code needs the same
   treatment via its own order list).

3. **Offline rescue first.** Before any server integration, the
   `layout_recover2.py` prototype can batch-process the known unverified
   0.3.x population from this corpus (and the Verifier Alliance backlog) to
   quantify exactly how many contracts it unblocks, and precompute the
   per-contract orderings. At ~1–8 s per contract this covers the whole
   mainnet 0.3.4–0.3.7 population in well under an hour.

Caveats:

- Layout recovery needs the on-chain bytecode as input, so it lives in the
  verifier, not the compiler.
- Compilation settings must be swept too: the CurveRouter case shows
  pre-berlin `evm_version` values (lock constants `0/1` vs `3/2`) masquerade
  as "nondeterminism". For 0.3.x candidates try `istanbul` in addition to the
  default.
- Identical-bodied internal functions are mutually ambiguous during decode,
  but any consistent assignment yields byte-identical output, so the final
  exact comparison is unaffected.
- The v2 mask (all code-symbol PUSH2 operands) is complete by construction;
  the v1 probe-diff mask could under-approximate and is retained only as a
  cross-check.

## Artifacts

- Prototypes (copied into this repo): **`scripts/layout_recover2.py`** (the
  chosen algorithm: 1 instrumented compile + offline decode + 1 verification
  compile), `scripts/layout_recover.py` (v1, 8-compile probe-diff variant),
  `scripts/permsearch.py` (k! enumeration), `scripts/greedysearch.py` (failed
  greedy, kept as negative result). All carry PEP 723 inline metadata
  (`vyper==0.3.7`, python `>=3.9,<3.12`), so they are self-contained:
  `uv run scripts/layout_recover2.py <source.vy> <onchain_runtime.hex>`.
- Scratch work dir (ephemeral): `/var/folders/zq/…/T/vy037/` — venvs for
  py3.8/3.9/3.10/3.11, official darwin/linux binaries, sources
  (`tricrypto.vy`, `drift1.vy` = Yearn Vault Factory, `drift2.vy` =
  CurveRouter), on-chain runtimes (`onchain_*.hex`), `check.py` match grader.
- Upstream: issue [#3369](https://github.com/vyperlang/vyper/issues/3369),
  fix [#3370](https://github.com/vyperlang/vyper/pull/3370) (`v0.3.8`),
  bug introduced in `4b44ee7` (`v0.3.4`).
