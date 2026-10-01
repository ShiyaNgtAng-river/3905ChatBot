from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


class ModelError(RuntimeError):
    pass


def json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        # Host providers have no JSON mode; tolerate a sentence around the object.
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        obj = json.loads(text[start : end + 1])
    if not isinstance(obj, dict):
        raise ModelError("模型必须输出 JSON 对象")
    return obj


class OpenAICompatible:
    """OpenAI-compatible HTTP transport; network destination comes only from configuration."""
    def __init__(self, settings: dict, usage=None):
        self.settings, self.usage = settings, usage
        self.model = settings.get("model", "")
        self.base = settings.get("base_url", "http://127.0.0.1:8000/v1").rstrip("/")
        parsed = urllib.parse.urlsplit(self.base)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.query:
            raise ValueError("模型地址无效")
        if not local and not settings.get("allow_remote", False):
            raise ValueError("远端模型需在配置中明确 allow_remote，并完成数据授权")
        if not local and parsed.scheme != "https":
            raise ValueError("远端模型必须使用 HTTPS")
        if not self.model:
            raise ValueError("未配置模型名称")
        # Learning-workspace extension: provider-specific thinking switches only.
        self.extra_body = settings.get("extra_body", {})
        if not isinstance(self.extra_body, dict) or set(self.extra_body) - {"enable_thinking", "thinking"}:
            raise ValueError("extra_body 仅支持 enable_thinking 和 thinking")
        if "enable_thinking" in self.extra_body and not isinstance(self.extra_body["enable_thinking"], bool):
            raise ValueError("enable_thinking 必须是布尔值")
        if "thinking" in self.extra_body and self.extra_body["thinking"] not in ({"type":"enabled"}, {"type":"disabled"}):
            raise ValueError("thinking 必须为 type=enabled 或 disabled")

    async def complete(self, system: str, payload, role: str, group: str,
                       timeout: float | None = None, max_tokens: int | None = None) -> str:
        """Send one system+user request; a str payload is sent verbatim so its prefix can hit the cache."""
        content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        body = {"model": self.model, "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content}
        ], "temperature": 0.2, "max_tokens": int(max_tokens or self.settings.get("max_tokens", 1800))}
        if self.settings.get("json_mode", True):
            body["response_format"] = {"type": "json_object"}
        body.update(self.extra_body)
        key = os.environ.get(self.settings.get("api_key_env", "GROUPBOT_MODEL_API_KEY"), "")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        started = time.monotonic()
        error = ""
        tokens = {}
        try:
            def request():
                req = urllib.request.Request(self.base + "/chat/completions", json.dumps(body).encode(), headers)
                # Redirects must not forward prompts or authorization to another host.
                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, *args, **kwargs):
                        return None
                with urllib.request.build_opener(NoRedirect()).open(req, timeout=float(timeout or self.settings.get("timeout", 40))) as r:
                    data = r.read(2_000_001)
                    if len(data) > 2_000_000:
                        raise ModelError("模型响应过长")
                    return json.loads(data)
            result = await asyncio.to_thread(request)
            usage = result.get("usage") or {}
            # DeepSeek reports prefix-cache hits itself; OpenAI-style servers use details.
            tokens = {"prompt_tokens": usage.get("prompt_tokens"),
                      "completion_tokens": usage.get("completion_tokens"),
                      "cached_tokens": usage.get("prompt_cache_hit_tokens",
                                                 (usage.get("prompt_tokens_details") or {}).get("cached_tokens"))}
            content = result["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ModelError("模型没有返回文本")
            return content
        except Exception as exc:
            error = type(exc).__name__  # Never persist a server response containing prompts/secrets.
            raise ModelError(f"模型服务调用失败：{error}") from None
        finally:
            if self.usage:
                self.usage(group, role, self.model, tokens, time.monotonic() - started, error)


class AstrBotProvider:
    def __init__(self, context, provider_id: str, usage=None):
        if not provider_id:
            raise ValueError("必须显式配置 AstrBot Provider ID")
        self.context, self.model, self.usage = context, provider_id, usage

    async def complete(self, system: str, payload, role: str, group: str,
                       timeout: float | None = None, max_tokens: int | None = None) -> str:
        """Call a host provider; max_tokens is accepted for parity but the host sets it."""
        started, error, tokens = time.monotonic(), "", {}
        try:
            result = await asyncio.wait_for(self.context.llm_generate(
                chat_provider_id=self.model, system_prompt=system,
                prompt=payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
            ), timeout=timeout or 45)
            usage = getattr(result, "usage", None)
            if usage is not None:
                cached = usage.input_cached
                raw = getattr(getattr(result, "raw_completion", None), "usage", None)
                if not cached and raw is not None:
                    cached = getattr(raw, "prompt_cache_hit_tokens", 0) or 0
                tokens = {"prompt_tokens": usage.input_other + usage.input_cached,
                          "completion_tokens": usage.output, "cached_tokens": cached}
            return result.completion_text
        except Exception as exc:
            error = type(exc).__name__
            raise ModelError(f"AstrBot Provider 调用失败：{error}") from None
        finally:
            if self.usage:
                self.usage(group, role, self.model, tokens, time.monotonic() - started, error)
