"""Client for the private, authenticated OpenAI-compatible endpoint (vLLM / SGLang).

Uses only the standard library (urllib in a worker thread) so the orchestrator has no
heavy HTTP dependency. A single asyncio.Semaphore caps concurrent model requests so
agents can never overload the one resident model.
"""
from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.request
from typing import Any, Optional

from .base import ChatMessage, ChatResult, ModelProvider, ProviderError, ProviderTimeout, ProviderUnavailable


class OpenAICompatProvider(ModelProvider):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model_id: str,
        *,
        max_concurrent: int = 4,
        timeout_s: float = 900.0,
        max_output_tokens: int = 8192,
    ):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model_id = model_id
        self.timeout_s = timeout_s
        self.max_output_tokens = max_output_tokens
        self._sem = asyncio.Semaphore(max(1, max_concurrent))
        self.in_flight = 0
        self.max_in_flight_seen = 0
        self.requests = 0
        self.failures = 0

    # ------------------------------------------------------------------ http
    def _request_sync(self, method: str, path: str, payload: Optional[dict], timeout: float) -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._api_key}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed private URL
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            if exc.code in (401, 403):
                raise ProviderError("model server rejected the credentials (check MODEL_API_KEY)", retryable=False) from None
            if exc.code == 429 or exc.code >= 500:
                raise ProviderUnavailable(f"model server returned HTTP {exc.code}") from None
            raise ProviderError(f"model server rejected the request (HTTP {exc.code}): {body}", retryable=False) from None
        except (socket.timeout, TimeoutError):
            raise ProviderTimeout(f"model request timed out after {timeout:.0f}s") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise ProviderTimeout(f"model request timed out after {timeout:.0f}s") from None
            raise ProviderUnavailable(f"cannot reach model server ({type(exc.reason).__name__})") from None
        except (ConnectionError, OSError) as exc:
            raise ProviderUnavailable(f"cannot reach model server ({type(exc).__name__})") from None
        except json.JSONDecodeError:
            raise ProviderError("model server returned invalid JSON", retryable=True) from None

    async def _request(self, method: str, path: str, payload: Optional[dict], timeout: float) -> Any:
        return await asyncio.to_thread(self._request_sync, method, path, payload, timeout)

    # ------------------------------------------------------------------ API
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        json_mode: bool = False,
    ) -> ChatResult:
        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": [m.as_dict() for m in messages],
            "max_tokens": min(int(max_tokens), self.max_output_tokens),
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        async with self._sem:
            self.in_flight += 1
            self.max_in_flight_seen = max(self.max_in_flight_seen, self.in_flight)
            self.requests += 1
            try:
                data = await asyncio.wait_for(
                    self._request("POST", "/chat/completions", payload, self.timeout_s), timeout=self.timeout_s + 10)
            except asyncio.TimeoutError:
                self.failures += 1
                raise ProviderTimeout(f"model request timed out after {self.timeout_s:.0f}s") from None
            except ProviderError:
                self.failures += 1
                raise
            finally:
                self.in_flight -= 1
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
            finish = choice.get("finish_reason") or "stop"
            usage = data.get("usage") or {}
        except (KeyError, IndexError, TypeError):
            self.failures += 1
            raise ProviderError("malformed response from model server", retryable=True) from None
        if not text.strip():
            self.failures += 1
            raise ProviderError("model returned an empty response", retryable=True)
        return ChatResult(text=text, prompt_tokens=int(usage.get("prompt_tokens", 0)),
                          completion_tokens=int(usage.get("completion_tokens", 0)), finish_reason=finish)

    async def health(self) -> tuple[bool, str]:
        try:
            data = await asyncio.wait_for(self._request("GET", "/models", None, 10), timeout=15)
        except ProviderError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return False, f"unreachable ({type(exc).__name__})"
        try:
            ids = [m["id"] for m in data["data"]]
        except (KeyError, TypeError):
            return False, "unexpected /models payload"
        if self.model_id not in ids:
            return False, f"model {self.model_id!r} is not being served (serving: {', '.join(ids[:3]) or 'nothing'})"
        return True, "ready"
