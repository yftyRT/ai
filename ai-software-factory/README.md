# AI Software Factory (Telegram → multi-agent build → tested ZIP)

One shared, private, self-hosted model (vLLM, OpenAI-compatible, API-key protected) on **one RunPod GPU Pod**.
A Telegram bot takes a project description, plans it, runs role-based agents concurrently, runs real QA in a
sandbox, repairs, reviews independently, packages a ZIP and sends it back.

## Honest status
* Tests: `79 passed, 0 failed` using a small pytest-compatible shim because PyPI was unreachable where this was built.
  Re-run with real pytest (`pip install -r requirements-dev.txt && pytest -q`).
* **Not exercised here:** the aiogram layer (`app/bot/telegram_app.py`), real vLLM + the model, RunPod, Docker/bwrap sandbox backends.
  Everything else is behind interfaces and tested with a fake model, fake messenger, local HTTP server and mocked GitHub.
* Costs shown by the bot are **estimates** (Pod hourly price × elapsed time). The real bill is in the RunPod console.
  The code never creates, resizes or terminates Pods.

## Deploy on RunPod (one Pod, ≤ $2/h)
1. Console → Pods → GPU: **A100 80GB** (listed ≈ $1.79/h on RunPod's pricing page at research time; availability and price change — check). Container disk 40 GB, **persistent volume 100–150 GB mounted at `/workspace`** (weights + DB + projects). Expose no public ports (health/vLLM bind locally; Telegram uses outbound polling).
2. Image: build from `Dockerfile` (`--build-arg BASE_IMAGE=vllm/vllm-openai:<verified tag>`), push to a registry, use it as the Pod image. Or use any CUDA Python image and run the manual steps below.
3. Pod environment variables: everything in `.env.example` (Secrets feature for tokens). Set `GPU_HOURLY_USD` to the price shown for *your* Pod incl. storage — startup refuses to run if it exceeds `GPU_BUDGET_USD_PER_HOUR`.
4. Start command: `supervisord -c /app/deploy/supervisord.conf` (runs vLLM and the factory as separate processes; each auto-restarts).

Manual (no Docker image):
```bash
git clone <your repo> /app && cd /app
pip install -r requirements.txt && pip install vllm   # vllm version: whichever supports your model; verify
cp .env.example .env && nano .env
set -a; . ./.env; set +a
nohup scripts/start_vllm.sh > /workspace/vllm.log 2>&1 &
python scripts/check_sandbox.py
python -m app.main
```
First start downloads ~54 GB of weights (BF16) — several minutes.

## BotFather
`/newbot` → copy token into `TELEGRAM_BOT_TOKEN`. Get your numeric id from @userinfobot → `AUTHORIZED_USER_IDS` and `ADMIN_USER_IDS`. Optionally `/setcommands` with: start, new, status, details, cancel, retry, logs, help, skills, skills_search, skills_discover, skills_review, skills_import, skills_update, skills_disable, skills_rollback. Unauthorized users get nothing.

## Verify
```bash
curl -s -H "Authorization: Bearer $MODEL_API_KEY" http://127.0.0.1:8000/v1/models
python scripts/healthcheck.py          # exit 0 only when model_ready is true
python scripts/check_sandbox.py        # read the isolation report honestly
```
Then in Telegram: `/start`, `/new`, send a small project, confirm the plan, wait for the ZIP.

## Usage
`/new` then describe the project → bot shows plan, skills, **estimated** cost → confirm → progress message (edited in place, rate-limited) → ZIP delivered and confirmed. `/status /details /logs /cancel /retry` per project.
Failed or partial builds are labelled `PARTIAL_SUCCESS / UNRESOLVED_ISSUES / FAILED` — never reported as success.

## GitHub Skills
Discovery/evaluation/import is admin-gated (`SKILL_TRUST_POLICY=manual` default). Imports pin the commit SHA, check license (copyleft refused unless `ALLOW_COPYLEFT_SKILLS`), scan for malicious content, stage, then activate; `/skills_rollback` restores the previous version; provenance is recorded and listed in `skills-used.md`. Skills are *instructions only*: they can never grant tools or file permissions (those live in code, `ROLE_POLICY`). Use a read-only `GITHUB_TOKEN` (60 → 5000 req/h). Imported text is untrusted — review before enabling in `manual` mode.

## Tests
```bash
pip install -r requirements-dev.txt && pytest -q
```

## Costs & stopping charges
* Always-on Pod ≈ hourly × 24 × 30 (e.g. $1.79 → ≈ $1,289/month) **plus storage** — this exceeds what most hobby use needs; consider running it only when needed.
* Stop vs terminate: a *stopped* Pod still bills for its volume/disk; *terminating* stops all Pod charges but deletes container disk (a network volume persists and keeps billing until deleted). To stop all charges: back up `/workspace/factory`, terminate the Pod, delete volumes, and confirm in Billing.
* Alternatives: on-demand Pod (start when needed; ~2–5 min model load), or Serverless (pay per second, cold starts; needs a different deploy path not included here).

## Troubleshooting
| Symptom | Check |
|---|---|
| Bot silent | token, your id in `AUTHORIZED_USER_IDS`, `factory.log` |
| `/status` says model not ready | `vllm.log` (OOM → lower `MAX_MODEL_LEN`/`MAX_NUM_SEQS` or `VLLM_QUANT=fp8`), `MODEL_ID` equals served name |
| Startup refuses: exceeds hard budget | `GPU_HOURLY_USD` > cap |
| QA "skipped" | sandbox backend limits, see `check_sandbox.py`; skipped ≠ passed |
| ZIP not delivered | bot reports it; `/retry` re-delivers without rebuilding |
