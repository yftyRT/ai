import pytest

from app.config import ConfigError, load_settings
from app.orchestrator.budget import BudgetExceeded, BudgetTracker
from tests.helpers import ADMIN, OTHER, OWNER, make_app, make_settings, run

BASE = {"TELEGRAM_BOT_TOKEN": "123456789:AAFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE", "AUTHORIZED_USER_IDS": "1",
        "MODEL_API_KEY": "k" * 20}


def test_valid_config_defaults():
    s = load_settings(BASE)
    assert s.gpu_hourly_usd <= s.gpu_budget_usd_per_hour == 2.0
    assert s.admin_user_ids == frozenset({1})
    assert s.validate_for_production() == []


def test_hourly_price_above_budget_is_rejected():
    with pytest.raises(ConfigError, match="exceeds the hard budget"):
        load_settings({**BASE, "GPU_HOURLY_USD": "2.09"})  # e.g. RTX PRO 6000 at the listed price


def test_remote_model_endpoint_rejected_by_default():
    with pytest.raises(ConfigError, match="self-hosted"):
        load_settings({**BASE, "MODEL_BASE_URL": "https://api.example.com/v1"})
    assert load_settings({**BASE, "MODEL_BASE_URL": "https://api.example.com/v1", "ALLOW_REMOTE_MODEL": "true"})


def test_invalid_values_and_no_secret_leak():
    secret = "123456789:SECRETSECRETSECRETSECRETSECRETSECRET"
    with pytest.raises(ConfigError) as ei:
        load_settings({**BASE, "TELEGRAM_BOT_TOKEN": secret, "MAX_ACTIVE_AGENTS": "0"})
    assert secret not in str(ei.value) and "MAX_ACTIVE_AGENTS" in str(ei.value)
    with pytest.raises(ConfigError):
        load_settings({**BASE, "AUTHORIZED_USER_IDS": "abc"})
    with pytest.raises(ConfigError):
        load_settings({**BASE, "ADMIN_USER_IDS": "99"})  # admin must be authorized
    with pytest.raises(ConfigError):
        load_settings({**BASE, "MAX_OUTPUT_TOKENS": "200000"})


def test_production_validation_flags_missing_secrets():
    s = load_settings({"AUTHORIZED_USER_IDS": "1", "PROVIDER": "fake"})
    problems = " ".join(s.validate_for_production())
    assert "TELEGRAM_BOT_TOKEN" in problems and "fake" in problems


def test_public_dict_has_no_secrets():
    s = load_settings({**BASE, "GITHUB_TOKEN": "ghp_" + "x" * 36})
    d = str(s.public_dict())
    assert BASE["TELEGRAM_BOT_TOKEN"] not in d and "ghp_" not in d and BASE["MODEL_API_KEY"] not in d
    assert "SECRET" not in repr(s) and BASE["MODEL_API_KEY"] not in repr(s)


def test_budget_tracker_refuses_over_cap(tmp_path):
    s = make_settings(tmp_path)
    object.__setattr__(s, "gpu_hourly_usd", 2.5)
    with pytest.raises(BudgetExceeded):
        BudgetTracker(s)


# ---------------------------------------------------------------- telegram authorization
def test_unauthorized_user_gets_nothing(tmp_path):
    async def go():
        app, msgr = await make_app(tmp_path)
        bot = app.bot
        for cmd in ("/start", "/new", "/status", "/details", "/cancel", "/retry", "/logs", "/help"):
            assert "not authorized" in await bot.handle_command(999, 1, cmd, "p1")
        assert "not authorized" in await bot.handle_text(999, 1, "build me a huge web application please")
        assert "not authorized" in await bot.handle_callback(999, "ok:p1")
        assert "not authorized" in await bot.handle_command(None, 1, "/help")
        assert await app.storage.list_projects() == []  # nothing was created
    run(go())


def test_users_cannot_see_or_control_each_others_projects(tmp_path):
    async def go():
        app, msgr = await make_app(tmp_path)
        await app.bot.handle_text(OWNER, 1, "Build a small python library that counts words in text files.")
        pid = (await app.storage.list_projects(OWNER))[0].id
        assert "Usage" in await app.bot.handle_command(OTHER, 2, "/details", pid)
        assert "Usage" in await app.bot.handle_command(OTHER, 2, "/logs", pid)
        assert "not found" in (await app.bot.handle_command(OTHER, 2, "/cancel", pid)).lower()
        assert "not found" in (await app.bot.handle_callback(OTHER, f"ok:{pid}")).lower()
        await app.orchestrator.wait(pid, 30)
    run(go())


def test_outgoing_text_is_redacted_and_commands_work(tmp_path):
    async def go():
        app, msgr = await make_app(tmp_path)
        token = app.settings.telegram_bot_token
        out = app.bot._out(f"leak {token} and key {app.settings.model_api_key} sk-abcdefghijklmnopqrstuvwxyz")
        assert token not in out and app.settings.model_api_key not in out and "sk-abc" not in out
        assert "/new" in await app.bot.handle_command(OWNER, 1, "/help")
        assert "Send the project description" in await app.bot.handle_command(OWNER, 1, "/new")
        assert "more detail" in await app.bot.handle_text(OWNER, 1, "hi")
        assert "Model server: ready" in await app.bot.handle_command(OWNER, 1, "/status")
        assert "Unknown command" in await app.bot.handle_command(OWNER, 1, "/nope")
    run(go())
