# Synthetic Verifier Benchmark Fixture

This directory is a deterministic smoke test for the verifier-benchmark data flow. It is not an empirical result and must not be cited as evidence that one verifier reduces reward hacking.

Regenerate every derived file from the fixed clean case:

```bash
uv run assessment-workbench benchmark attack \
  --cases examples/verifier-benchmark/clean.jsonl \
  --output examples/verifier-benchmark/cases.jsonl

uv run python examples/verifier-benchmark/generate_synthetic_observations.py \
  --cases examples/verifier-benchmark/cases.jsonl \
  --output examples/verifier-benchmark/observations.synthetic.jsonl

uv run assessment-workbench benchmark report \
  --cases examples/verifier-benchmark/cases.jsonl \
  --observations examples/verifier-benchmark/observations.synthetic.jsonl \
  --verifier surface_checker \
  --verifier specialized_ensemble \
  --output examples/verifier-benchmark/report.synthetic.json
```

The fixture checks these contracts end to end:

- one independently labeled clean Bundle;
- all six controlled attack families with closed version lineage;
- exact content-version binding for every Verifier observation;
- per-family detection and escape rates;
- ensemble disagreement AUROC;
- best-of-N optimization-pressure curves when reward candidates are present.

The two Verifier names describe deterministic rule tables in `generate_synthetic_observations.py`. They do not call an LLM, and all generated observations record `model="synthetic-fixture"`.
