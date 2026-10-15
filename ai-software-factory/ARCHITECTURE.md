# Architecture
```
Telegram ─ BotService (auth, redaction) ─ Orchestrator ─ ProjectManager(plan, skills)
                                              │ TaskScheduler (asyncio, deps, retries, limits)
                                              │ Agent(role+skills) ─ one shared Provider (vLLM)
                                              │ WorkspaceManager (per-task dirs, ownership) → integrate()
                                              │ Sandbox + QARunner → repair loop → ReviewerAgent
                                              │ reporting → packaging(ZIP, validate, split) → delivery
Storage (SQLite behind a Protocol)   Messenger (Telegram behind a Protocol)   Budget/Estimator/Progress
```
* **Process split:** vLLM and the factory are separate processes; a model crash is retried by the scheduler, not fatal.
* **Concurrency:** `MAX_ACTIVE_AGENTS` tasks at once, `MAX_CONCURRENT_MODEL_REQUESTS` in-flight model calls (semaphore).
* **Isolation:** each task writes in its own directory and may only own files matching its `owned_files` globs (checked against `ROLE_POLICY` in code). `integrate()` merges deterministically; owner wins conflicts; unresolved ones are reported. *Deviation from spec wording:* task directories instead of git worktrees — simpler and works without git in the sandbox.
* **State/recovery:** every state transition is persisted before work starts; on boot interrupted tasks reset to PENDING and the project resumes; completed work isn't redone.
* **Quality gate:** QA results are facts from real command exit codes; skipped checks are listed as skipped. Final status derives from them plus the independent reviewer (separate prompt/skill, sees artifacts not the author's reasoning).
* **Storage migration:** `app/storage/base.py` defines the Protocol (documents + projects/tasks/events/usage). To move to PostgreSQL implement the same Protocol (e.g. asyncpg) and swap in `container.py`; schema is JSON-document based with indexed ids, so migration is a table copy.
* **Budget:** `BudgetTracker` separates *estimated* (hourly × time, tokens × price) from *billed* (unknown to code). It refuses configs above the cap and stops scheduling when the per-project budget is reached. No code path provisions GPUs.
* **Security:** secrets only from env, redacted from logs/Telegram; child processes get a scrubbed env; `CommandPolicy` forbids shell metacharacters/installs by default; sandbox network off by default (enforced only if the backend supports it — see `check_sandbox.py`); ZIP excludes secrets/venvs/weights/caches and is re-validated.
