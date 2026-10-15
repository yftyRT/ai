#!/usr/bin/env bash
# Starts the PRIVATE model server. Binds to 127.0.0.1 only; the factory talks to it locally.
# Flags follow the vLLM docs for Qwen3.x as I understand them; flag names change between vLLM
# releases - if vLLM rejects one, run `vllm serve --help` and adjust. NOT tested in the build sandbox.
set -euo pipefail
: "${MODEL_API_KEY:?set MODEL_API_KEY (same value as in .env)}"
MODEL="${MODEL_ID:-Qwen/Qwen3.6-27B}"
QUANT_ARGS=()
# A100 80GB: BF16 27B = ~54GB weights. FP8 halves that and leaves more KV-cache; set VLLM_QUANT=fp8 to use it.
[ "${VLLM_QUANT:-}" = "fp8" ] && QUANT_ARGS=(--quantization fp8)
export HF_HOME="${HF_HOME:-/workspace/hf}"   # weights cached on the persistent volume
exec vllm serve "$MODEL" \
  --host 127.0.0.1 --port 8000 \
  --api-key "$MODEL_API_KEY" \
  --served-model-name "$MODEL" \
  --max-model-len "${MAX_MODEL_LEN:-131072}" \
  --max-num-seqs "${MAX_NUM_SEQS:-4}" \
  --gpu-memory-utilization "${GPU_UTIL:-0.90}" \
  --enable-prefix-caching \
  "${QUANT_ARGS[@]}"
