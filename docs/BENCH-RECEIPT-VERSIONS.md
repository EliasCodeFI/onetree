# Benchmark receipt compatibility

New experimental runs write `onetree-bench-v4`. This combines the accepted
SeedSequence corpus with the accepted commit-inclusion, observed-byte-lifetime,
closing-census and manifest corrections. It does not amend the frozen benchmark
protocol, authorize a formal run, or settle the pending grid and hot-pool policy.

The complete format identity is the schema **and** the recorded seed derivation.
Both `run.json` and `FREEZE.json` must agree before any scoring or manifest
interpretation takes place. Unknown or inconsistent pairs are void; no fallback
is chosen from whichever files happen to verify.

| Schema | Derivation | Corpus | Events and collisions | Manifest exclusions |
| --- | --- | --- | --- | --- |
| v2 | Historical SHA string | Original SHA corpus | Historical v2 | Historical broad exclusions |
| v3 | Historical SHA string | Original SHA corpus | Accepted lifecycle contract | Narrow exclusions |
| v3 | Accepted A1 SeedSequence string | Accepted A1 corpus | Historical v2 | Historical broad exclusions |
| v4 | Accepted A1 SeedSequence string | Accepted A1 corpus | Accepted lifecycle contract | Narrow exclusions |

The strings above are shorthand for the full `DERIVATION` constants in
`legacy_seeds.py` and `seeds.py`. There are no aliases or prefix matches.
The two v3 rows preserve the formats of separate, unpublished accepted candidates
that used the same schema name. They are not interchangeable reproductions.
`compare` refuses counts across schemas or derivations, even if the seed, grid
and systems match. Original receipts are never rewritten or automatically upgraded.

Narrow exclusions omit only the root `FREEZE.json` and `MANIFEST.sha256`, and
files within root `tmp/` and `repos/` directories. Regular root files named `tmp`
or `repos`, and nested files named `FREEZE.json` or `MANIFEST.sha256`, are covered.
Historical broad exclusions retain the former first-component and filename rules.

For lifecycle receipts, any observed staged copy preserves byte presence. A write's
uncommitted interval ends at observed inclusion in any Git commit, or when its bytes
disappear under the accepted observation rules. Incomplete closing evidence stays
uncertain; unknown counts must not be rendered as zero. Lost-work accounting remains
separate. The integration retains the accepted scorer and event producers unchanged.

The retained fixtures in `tests/fixtures/bench-history` were produced by the accepted
predecessors. Tests verify their original manifests and results without rewriting
them, check the two v3 interpretations, and reject relabelled or inconsistent inputs.
The accepted A1 reference vectors remain in `tests/test_bench_seedsequence.py`.

This compatibility table does not certify every experimental intermediate candidate.
It pins the accepted A1 and task200-r3 formats plus the published v2 contract.
Malformed staged-corpus policy (task211), the grid amendment (task25), formal benchmark
results and Gate3 closure remain separate obligations. Existing experimental receipts
are not promoted into preregistered results by this integration.
