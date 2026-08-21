# Frozen P5-AI-A canonical model

Deterministic float32 3->8->1 MLP (ReLU hidden, sigmoid output), IF-008-v1
normalization, Q16.16 duty. Exported by scripts/contest/ai/{generate_dataset,
train_export}.py with a fixed seed; golden vectors 256; Python-vs-C cross-check
passes (models are byte-consistent: model_sha256 2ba04889...).

- model.bin            164-byte canonical weights (W1,b1,W2,b2, LE f32)
- metadata.json        shape / normalization / quantization / training stats
- golden-vectors.json  256 {input[3], expected_duty_q16_16}
- checksums.sha256     model.bin + metadata.json + golden-vectors.json

The deployment C inference in ../src/model mirrors these vectors (see
scripts/test/check_contest_ai_golden_c.py, CONTEST_AI_GOLDEN_C_PASS).
This is data (frozen model), not a build artifact.
