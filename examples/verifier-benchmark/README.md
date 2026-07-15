# Verifier Benchmark Fixture

This directory is a byte-reproducible smoke test for the verifier-benchmark data flow. It contains executable deterministic baselines and separately labeled synthetic rule-table observations. The single clean case and six controlled attacks are not a publishable empirical corpus.

Regenerate every derived file from the fixed clean case:

```bash
uv run assessment-workbench benchmark attack \
  --cases examples/verifier-benchmark/clean.jsonl \
  --output examples/verifier-benchmark/cases.jsonl

uv run assessment-workbench benchmark observe-baseline \
  --cases examples/verifier-benchmark/cases.jsonl \
  --output examples/verifier-benchmark/observations.baseline.jsonl

uv run assessment-workbench benchmark report \
  --cases examples/verifier-benchmark/cases.jsonl \
  --observations examples/verifier-benchmark/observations.baseline.jsonl \
  --verifier schema_only \
  --verifier structure \
  --output examples/verifier-benchmark/report.baseline.json

uv run python examples/verifier-benchmark/generate_synthetic_observations.py \
  --cases examples/verifier-benchmark/cases.jsonl \
  --output examples/verifier-benchmark/observations.synthetic.jsonl

uv run assessment-workbench benchmark report \
  --cases examples/verifier-benchmark/cases.jsonl \
  --observations examples/verifier-benchmark/observations.synthetic.jsonl \
  --verifier surface_checker \
  --verifier specialized_ensemble \
  --output examples/verifier-benchmark/report.synthetic.json

uv run assessment-workbench benchmark export-episodes \
  --cases examples/verifier-benchmark/cases.jsonl \
  --observations examples/verifier-benchmark/observations.baseline.jsonl \
  --output examples/verifier-benchmark/episodes.baseline.jsonl

uv run assessment-workbench benchmark export-preferences \
  --cases examples/verifier-benchmark/cases.jsonl \
  --observations examples/verifier-benchmark/observations.baseline.jsonl \
  --verifier schema_only \
  --verifier structure \
  --output examples/verifier-benchmark/preferences.baseline.jsonl
```

The executable `schema_only` and `structure` baselines both accept all six schema-valid semantic attacks: recall `0.0`, attack success rate `1.0`, and disagreement AUROC `0.5`. This is a real result for this controlled fixture and demonstrates that structural validity is not semantic verification. It does not establish performance on a larger expert-labeled corpus.

The fixture checks these contracts end to end:

- one independently labeled clean Bundle;
- all six controlled attack families with closed version lineage;
- exact content-version binding for every Verifier observation;
- per-family detection and escape rates;
- ensemble disagreement AUROC;
- best-of-N optimization-pressure curves when reward candidates are present.
- RLVR episode and clean-versus-attacked environment preference exports.

The two Verifier names describe deterministic rule tables in `generate_synthetic_observations.py`. They do not call an LLM, and all generated observations record `model="synthetic-fixture"`.
