# Model selection (research notes — verify before relying)
Chosen: **Qwen3.6-27B** (Apache-2.0, dense, long context) served by vLLM (≥ 0.19 per my notes).
Why: fits one 80 GB GPU in BF16 (~54 GB weights) leaving KV-cache room; permissive license; strong coding results *as reported by its developers*.
Caveats: benchmark numbers are developer-reported with differing harnesses and were not reproduced by me; the KV-cache/concurrency arithmetic (`--max-num-seqs 4` at 131k context) is my estimate, not a measurement — lower `MAX_MODEL_LEN` or use `VLLM_QUANT=fp8` if you hit OOM.
Alternatives considered: Qwen3-Coder-Next (80B-A3B MoE: needs more VRAM or quantization), DeepSeek V4.1 Flash (552B: does not fit one GPU).
GPU pricing seen: A100 80GB $1.79/h (RunPod page) vs $1.59 (third-party listing); RTX PRO 6000 $2.49 / $2.09 — over the $2 cap. Availability not verified. Re-check the console.
