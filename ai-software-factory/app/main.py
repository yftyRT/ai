"""Process entry point: Telegram bot + orchestrator in one asyncio process.
The model server (vLLM) is a SEPARATE process, so a model crash never kills the bot."""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time

from app.config import ConfigError, load_settings
from app.util import redact


class JsonFormatter(logging.Formatter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = secrets

    def format(self, record: logging.LogRecord) -> str:
        msg = redact(record.getMessage(), self.secrets)
        if record.exc_info:
            msg += " | " + redact(self.formatException(record.exc_info), self.secrets)
        return json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
                           "level": record.levelname, "logger": record.name, "msg": msg})


async def health_server(app, port: int) -> asyncio.AbstractServer:
    """Minimal HTTP health endpoint: 200 when the process is alive; body reports model readiness."""
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(reader.readline(), 5)
            ok, detail = await app.provider.health()
            body = json.dumps({"app": "ok", "model_ready": ok, "model": detail, "sandbox": app.sandbox.capabilities()}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", port)


async def amain() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    problems = settings.validate_for_production()
    if problems:
        print("refusing to start:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(settings.secret_values()))
    logging.basicConfig(level=settings.log_level, handlers=[handler])
    log = logging.getLogger("factory")

    from app.bot.telegram_app import AiogramMessenger, build_dispatcher, create_bot
    from app.container import build_app

    bot = create_bot(settings.telegram_bot_token)
    app = build_app(settings, AiogramMessenger(bot))
    await app.storage.init()
    recovered = await app.orchestrator.recover()
    log.info("started; recovered %d interrupted project(s); sandbox=%s", recovered, app.sandbox.capabilities())
    health = await health_server(app, settings.health_port)
    dp = build_dispatcher(app.bot)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    poll = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    await stop.wait()
    log.info("shutting down gracefully")
    await dp.stop_polling()
    poll.cancel()
    await app.orchestrator.shutdown()  # task state is persisted; recover() resumes it on next start
    health.close()
    await app.storage.close()
    await bot.session.close()
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
