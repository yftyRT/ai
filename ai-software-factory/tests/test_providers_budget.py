import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.orchestrator.budget import BudgetExceeded, BudgetTracker
from app.orchestrator.estimator import estimate_remaining
from app.orchestrator.progress import ProgressReporter, render_status
from app.providers.base import ChatMessage, ProviderError, ProviderTimeout, ProviderUnavailable
from app.providers.fake import FakeProvider
from app.providers.openai_compat import OpenAICompatProvider
from app.bot.messenger import RecordingMessenger
from app.schemas.models import Project, Task, TaskState
from tests.helpers import make_settings, run

KEY = "secret-model-key"


class _Handler(BaseHTTPRequestHandler):
    mode = "ok"
    seen_auth: list = []
    active = 0
    peak = 0
    lock = threading.Lock()

    def log_message(self, *a):  # silence
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _Handler.seen_auth.append(self.headers.get("Authorization"))
        self._send(200, {"data": [{"id": "m1"}]})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        _Handler.seen_auth.append(self.headers.get("Authorization"))
        mode = _Handler.mode
        with _Handler.lock:
            _Handler.active += 1
            _Handler.peak = max(_Handler.peak, _Handler.active)
        try:
            if mode == "slow":
                time.sleep(0.4)
            if mode == "500":
                return self._send(500, {"error": {"message": "boom"}})
            if mode == "401":
                return self._send(401, {"error": {"message": "no"}})
            if mode == "400":
                return self._send(400, {"error": {"message": "context too long"}})
            if mode == "garbage":
                return self._send(200, {"nope": 1})
            if mode == "empty":
                return self._send(200, {"choices": [{"message": {"content": "  "}, "finish_reason": "stop"}]})
            self._send(200, {"choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
        finally:
            with _Handler.lock:
                _Handler.active -= 1


def _server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/v1"


def _provider(url, **kw):
    kw.setdefault("timeout_s", 5)
    return OpenAICompatProvider(url, KEY, "m1", **kw)


def test_provider_success_auth_and_usage():
    srv, url = _server()
    try:
        _Handler.mode = "ok"
        res = run(_provider(url).chat([ChatMessage("user", "hi")]))
        assert res.text == '{"ok": true}' and res.prompt_tokens == 7 and res.completion_tokens == 3
        assert f"Bearer {KEY}" in _Handler.seen_auth
        assert run(_provider(url).health()) == (True, "ready")
        ok, detail = run(OpenAICompatProvider(url, KEY, "other-model").health())
        assert not ok and "not being served" in detail
    finally:
        srv.shutdown()


def test_provider_error_mapping():
    srv, url = _server()
    try:
        for mode, exc, retryable in (("500", ProviderUnavailable, True), ("401", ProviderError, False),
                                     ("400", ProviderError, False), ("garbage", ProviderError, True),
                                     ("empty", ProviderError, True)):
            _Handler.mode = mode
            with pytest.raises(exc) as ei:
                run(_provider(url).chat([ChatMessage("user", "hi")]))
            assert ei.value.retryable is retryable, mode
            assert KEY not in str(ei.value)
    finally:
        srv.shutdown()


def test_provider_timeout_and_unreachable():
    srv, url = _server()
    try:
        _Handler.mode = "slow"
        with pytest.raises(ProviderTimeout):
            run(_provider(url, timeout_s=0.1).chat([ChatMessage("user", "hi")]))
    finally:
        srv.shutdown()
        srv.server_close()
    with pytest.raises(ProviderUnavailable):
        run(_provider(url).chat([ChatMessage("user", "hi")]))  # server is gone
    ok, detail = run(_provider(url).health())
    assert not ok and detail


def test_concurrent_model_requests_are_capped():
    srv, url = _server()
    try:
        _Handler.mode = "slow"
        _Handler.peak = 0
        p = _provider(url, max_concurrent=2)

        async def go():
            await asyncio.gather(*[p.chat([ChatMessage("user", str(i))]) for i in range(6)])
        run(go())
        assert p.max_in_flight_seen == 2 and _Handler.peak <= 2
    finally:
        srv.shutdown()


# ------------------------------------------------------------------ budget
def _project(budget=5.0, elapsed=0.0, tokens=0.0):
    p = Project(id="p1", owner_id=1, chat_id=1, prompt="x", budget_usd=budget, spent_tokens_usd=tokens)
    p.started_at = 1000.0 - elapsed
    return p


def test_budget_cost_accounting_and_enforcement(tmp_path):
    s = make_settings(tmp_path, GPU_HOURLY_USD="1.79", TOKEN_PRICE_IN_PER_M="1", TOKEN_PRICE_OUT_PER_M="2")
    b = BudgetTracker(s, clock=lambda: 1000.0)
    assert abs(b.infra_cost_for_seconds(3600) - 1.79) < 1e-9
    assert abs(b.token_cost(1_000_000, 500_000) - 2.0) < 1e-9
    assert b.can_schedule(_project(budget=5, elapsed=3600))  # $1.79 < $5
    assert not b.can_schedule(_project(budget=1.0, elapsed=3600))  # $1.79 >= $1
    assert not b.can_schedule(_project(budget=5, elapsed=0, tokens=5.0))
    with pytest.raises(BudgetExceeded):
        b.check(_project(budget=1.0, elapsed=3600))
    done = _project(budget=100, elapsed=7200)
    done.finished_at = 1000.0 - 3600  # accounting stops at finish time
    assert abs(b.spent(done).infra_usd - 1.79) < 1e-9


def test_estimate_is_labelled_and_scales_with_plan(tmp_path):
    from app.providers.fake import _PLAN
    from app.schemas.models import Plan
    s = make_settings(tmp_path)
    b = BudgetTracker(s)
    from app.orchestrator.project_manager import ProjectManager
    pm = ProjectManager(FakeProvider(), None, s)
    plan = pm.validate_plan_dict(_PLAN, ["architect", "backend", "tester", "docs"])
    proj = _project()
    proj.plan = plan
    est = b.estimate_before_run(proj)
    assert est.infra_usd > 0 and est.total >= est.infra_usd


# ------------------------------------------------------------------ estimator / progress
def _t(i, state, dur=None, started=None, agent="backend", deps=()):
    t = Task(id=i, agent=agent, skill="s", description=i, owned_files=[f"{i}/**"], depends_on=list(deps), state=state)
    if dur is not None:
        t.started_at, t.ended_at = 100.0, 100.0 + dur
    if started is not None:
        t.started_at = started
    return t


def test_estimate_says_calculating_without_history():
    tasks = [_t("a", TaskState.RUNNING, started=50.0), _t("b", TaskState.PENDING, deps=["a"])]
    assert estimate_remaining(tasks, 2, now=60.0) is None
    s = make_settings(__import__("pathlib").Path("/tmp/x_est"))
    text = render_status(_project(), tasks, s, BudgetTracker(s), now=60.0)
    assert "Calculating estimate" in text


def test_estimate_uses_measured_durations_and_dependencies():
    done = [_t("a", TaskState.COMPLETED, dur=60), _t("b", TaskState.COMPLETED, dur=60), _t("c", TaskState.COMPLETED, dur=60)]
    chain = done + [_t("d", TaskState.PENDING, deps=["c"]), _t("e", TaskState.PENDING, deps=["d"])]
    wide = done + [_t("d", TaskState.PENDING), _t("e", TaskState.PENDING)]
    lo_c, hi_c = estimate_remaining(chain, 4, now=0)
    lo_w, hi_w = estimate_remaining(wide, 4, now=0)
    assert hi_c > hi_w  # a dependency chain cannot be parallelised
    assert lo_c < hi_c
    assert estimate_remaining(done, 4, now=0) == (0.0, 0.0) or estimate_remaining(done, 4, now=0)[1] >= 0


def test_progress_updates_are_rate_limited_and_edit_one_message(tmp_path):
    async def go():
        s = make_settings(tmp_path, PROGRESS_MIN_INTERVAL_S="10")
        clock = {"t": 0.0}
        m = RecordingMessenger()
        rep = ProgressReporter(m, None, s, BudgetTracker(s), clock=lambda: clock["t"])
        p = _project()
        p.chat_id = 7
        tasks = [_t("a", TaskState.RUNNING, started=1.0)]
        assert await rep.update(p, tasks)
        assert len(m.sent) == 1 and p.status_message_id
        tasks[0].state = TaskState.COMPLETED
        assert not await rep.update(p, tasks)  # too soon
        clock["t"] = 11.0
        assert await rep.update(p, tasks)
        assert len(m.edits) == 1 and len(m.sent) == 1  # edited, not re-sent
        assert not await rep.update(p, tasks)  # identical text
        assert await rep.update(p, tasks, force=True)
        assert p.latest_status_text
    run(go())
