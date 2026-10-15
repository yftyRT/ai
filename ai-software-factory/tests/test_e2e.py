"""End-to-end: Telegram text -> plan -> confirm -> build -> real QA in the sandbox -> review -> ZIP -> delivery."""
import zipfile

from app.schemas.models import ProjectState, TaskState
from app.providers.fake import FakeProvider
from tests.helpers import OWNER, make_app, run, settle

PROMPT = "Build a small python library and CLI that counts characters, words and lines of a text."


async def _build(tmp_path, provider=None, **over):
    app, msgr = await make_app(tmp_path, provider=provider, **over)
    reply = await app.bot.handle_text(OWNER, 5, PROMPT)
    assert "created" in reply
    pid = (await app.storage.list_projects(OWNER))[0].id
    project = await settle(app, pid)  # planning
    assert project.state == ProjectState.AWAITING_CONFIRMATION, project.error
    assert msgr.sent[-1]["buttons"], "plan must be shown with confirmation buttons"
    assert "ESTIMATE" in msgr.sent[-1]["text"]
    out = await app.bot.handle_callback(OWNER, f"ok:{pid}")
    assert "Started" in out
    project = await settle(app, pid)
    return app, msgr, project


def test_full_pipeline_success(tmp_path):
    async def go():
        app, msgr, p = await _build(tmp_path)
        assert p.state == ProjectState.SUCCESS, (p.state, p.notes, p.error, p.test_report and [(c.name, c.passed, c.output) for c in p.test_report.checks])
        assert p.delivered and p.artifact_path
        assert msgr.documents and msgr.documents[0]["path"].name.endswith(".zip")
        names = zipfile.ZipFile(p.artifact_path).namelist()
        for need in ("README.md", "ARCHITECTURE.md", "test-report.md", ".env.example", "requirements.txt", "BUILD_SUMMARY.md"):
            assert f"textstats/{need}" in names, need
        assert not any(n.endswith(".pyc") or "/.env" == n[-5:] for n in names)
        tasks = await app.storage.get_tasks(p.id)
        assert all(t.state == TaskState.COMPLETED for t in tasks)
        assert [c.name for c in p.test_report.checks if not c.skipped and c.passed], "real checks must have run"
        assert any(c.name.startswith("tests") and c.passed for c in p.test_report.checks)
        assert "Archive delivered" in msgr.sent[-1]["text"]
        # progress updates were rate-limited edits of one message
        assert msgr.edits
    run(go())


def test_repair_loop_fixes_real_test_failure(tmp_path):
    async def go():
        fp = FakeProvider()
        fp.broken_once.add("core")  # first output has a real bug that only the tests reveal
        app, msgr, p = await _build(tmp_path, provider=fp)
        assert p.repair_cycles >= 1
        assert p.state == ProjectState.SUCCESS, (p.state, p.notes)
        assert any("REPAIR REQUEST" in c for c in fp.calls)
        report = p.test_report
        assert report.passed
    run(go())


def test_model_down_never_reports_success(tmp_path):
    async def go():
        fp = FakeProvider()
        app, msgr = await make_app(tmp_path, provider=fp, MAX_RETRIES="1")
        await app.bot.handle_text(OWNER, 5, PROMPT)
        pid = (await app.storage.list_projects(OWNER))[0].id
        await settle(app, pid)
        fp.healthy = False  # model dies after planning
        await app.bot.handle_callback(OWNER, f"ok:{pid}")
        p = await settle(app, pid)
        assert p.state == ProjectState.FAILED
        tasks = await app.storage.get_tasks(pid)
        assert not any(t.state == TaskState.COMPLETED for t in tasks)
        assert not p.delivered
    run(go())
