from __future__ import annotations

import base64
import binascii
import hashlib
import importlib.util
import io
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

MAX_FILE = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
CAPABILITIES = {"ocr", "describe", "transcribe", "generate"}
MIMES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/flac": ".flac",
    "audio/ogg": ".ogg",
}


class MediaError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def default_config():
    return {
        "ocr": {"backend": "apple_vision"},
        "describe": {"backend": "disabled"},
        "transcribe": {"backend": "faster_whisper", "model_path": "", "language": "zh"},
        "generate": {"backend": "disabled"},
    }


def _run(argv, timeout):
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise MediaError("timeout", "本地媒体处理超时，未完成。", 504) from None
    except OSError:
        raise MediaError("dependency_missing", "本地处理依赖不可用。", 503) from None
    if p.returncode:
        raise MediaError("local_processing_failed", "本地媒体处理失败。", 502)
    return p.stdout


def _key(cfg):
    value = os.environ.get(str(cfg.get("api_key_env", "")), "")
    if not value:
        raise MediaError("not_configured", "尚未配置该媒体服务的 API Key。", 503)
    return value


def _endpoint(cfg, path):
    base = str(cfg.get("base_url", "")).rstrip("/")
    parts = urlsplit(base)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise MediaError(
            "invalid_config", "云服务地址必须为无凭据的 HTTPS 基础地址。", 503
        )
    return base + path


def _image_check(data):
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.width * img.height > MAX_IMAGE_PIXELS or img.format not in {
                "PNG",
                "JPEG",
                "WEBP",
            }:
                raise MediaError("invalid_image", "图片尺寸或格式超出支持范围。")
            fmt = img.format
            img.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise MediaError("invalid_image", "无法解析图片。") from None
    return {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[fmt]


class MediaService:
    def __init__(self, config, home: Path, *, client=None):
        self.config = {**default_config(), **config}
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.client = client or httpx.Client(
            timeout=90, follow_redirects=False, trust_env=False
        )
        self._build_lock = threading.Lock()
        self._asr_gate = threading.Semaphore(1)

    def close(self):
        self.client.close()

    def capabilities(self):
        output = {}
        for name in sorted(CAPABILITIES):
            cfg = self.config[name]
            backend = cfg.get("backend", "disabled")
            if backend == "apple_vision":
                ready = platform.system() == "Darwin" and bool(shutil.which("swiftc"))
                reason = "ready" if ready else "requires_macos_swift"
            elif backend == "faster_whisper":
                model = Path(cfg.get("model_path") or "__not_configured__")
                ready = (
                    bool(importlib.util.find_spec("faster_whisper"))
                    and (model / "model.bin").is_file()
                )
                reason = "ready" if ready else "requires_local_model"
            elif backend in {
                "vision_chat",
                "audio_transcriptions",
                "siliconflow_images",
                "dashscope_image",
                "openai_images",
            }:
                ready = (
                    bool(os.environ.get(str(cfg.get("api_key_env", ""))))
                    and bool(cfg.get("model"))
                    and bool(cfg.get("base_url"))
                )
                reason = (
                    "configured_not_live_verified"
                    if ready
                    else "requires_api_configuration"
                )
            else:
                ready, reason = False, "disabled"
            output[name] = {
                "backend": backend,
                "available": ready,
                "status": reason,
                "processing": "local"
                if backend in {"apple_vision", "faster_whisper"}
                else "remote",
            }
        return {
            "schema": "groupmedia-capabilities-v1",
            "capabilities": output,
            "stores_media": False,
            "writes_group_memory": False,
        }

    def _decode(self, request, kind):
        obj = request.get("media")
        if not isinstance(obj, dict) or not isinstance(obj.get("base64"), str):
            raise MediaError(
                "invalid_media", "需要 base64 媒体内容，不能传服务器路径或下载地址。"
            )
        if len(obj["base64"]) > (MAX_FILE * 4 // 3 + 8):
            raise MediaError("too_large", "文件超过 20 MiB。", 413)
        mime = obj.get("mime", "")
        if mime not in MIMES or not mime.startswith(kind + "/"):
            raise MediaError("invalid_media", "媒体类型不匹配。")
        try:
            data = base64.b64decode(obj["base64"], validate=True)
        except (ValueError, binascii.Error):
            raise MediaError("invalid_media", "base64 格式错误。") from None
        if not data or len(data) > MAX_FILE:
            raise MediaError("too_large", "文件为空或超过 20 MiB。", 413)
        if kind == "image":
            mime = _image_check(data)
        return data, mime

    def process(self, operation, request):
        started = time.monotonic()
        if operation not in CAPABILITIES or not isinstance(request, dict):
            raise MediaError("invalid_request", "未知媒体操作。")
        source = request.get("source", {})
        if not isinstance(source, dict) or not all(
            isinstance(source.get(k), str) and 0 < len(source[k]) <= 200
            for k in ["group", "message_id"]
        ):
            raise MediaError(
                "invalid_source", "需要 group 和 message_id，以便关联识别来源。"
            )
        if (
            not isinstance(request.get("prompt", ""), str)
            or len(request.get("prompt", "")) > 4000
        ):
            raise MediaError("invalid_prompt", "提示词过长或格式错误。")
        cfg = self.config[operation]
        backend = cfg.get("backend", "disabled")
        valid = {
            "ocr": {"apple_vision", "vision_chat"},
            "describe": {"vision_chat"},
            "transcribe": {"faster_whisper", "audio_transcriptions"},
            "generate": {"siliconflow_images", "dashscope_image", "openai_images"},
        }
        if backend not in valid[operation]:
            raise MediaError("not_configured", "该多模态能力尚未配置。", 503)
        if backend not in {"apple_vision", "faster_whisper"} and not cfg.get("model"):
            raise MediaError("invalid_config", "尚未配置媒体模型名称。", 503)
        data, mime = (
            (b"", "")
            if operation == "generate"
            else self._decode(
                request, "audio" if operation == "transcribe" else "image"
            )
        )
        # Each operation has its own ephemeral directory, with no cross-group cache.
        with tempfile.TemporaryDirectory(prefix="media-", dir=self.home) as tmp:
            path = Path(tmp) / ("input" + MIMES[mime]) if data else None
            if path:
                path.write_bytes(data)
            if backend == "apple_vision":
                result = self._ocr(path)
            elif backend == "faster_whisper":
                result = self._transcribe(path, cfg)
            elif backend == "vision_chat":
                result = self._vision(data, mime, request, cfg, operation)
            elif backend == "audio_transcriptions":
                result = self._remote_asr(data, mime, cfg)
            else:
                result = self._generate(request, cfg)
        return {
            "schema": "groupmedia-result-v1",
            "ok": True,
            "operation": operation,
            "backend": backend,
            **result,
            "source": {
                "group": source["group"],
                "message_id": source["message_id"],
                "sha256": hashlib.sha256(data).hexdigest() if data else None,
            },
            "derived": True,
            "memory_written": False,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }

    def _ocr(self, path):
        if platform.system() != "Darwin" or not shutil.which("swiftc"):
            raise MediaError(
                "dependency_missing",
                "本地 OCR 需要 macOS 和 Swift Command Line Tools。",
                503,
            )
        source = Path(__file__).with_name("vision_ocr.swift")
        version = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        binary = self.home / ("vision-ocr-" + version)
        with self._build_lock:
            if not binary.exists():
                _run(
                    [
                        "swiftc",
                        "-module-cache-path",
                        str(self.home / "swift-cache"),
                        str(source),
                        "-o",
                        str(binary),
                    ],
                    timeout=120,
                )
        raw = _run([str(binary), str(path)], timeout=45)
        try:
            return json.loads(raw)
        except ValueError:
            raise MediaError("invalid_result", "OCR 未返回有效结果。", 502) from None

    def _transcribe(self, path, cfg):
        model = (
            Path(cfg.get("model_path") or "__not_configured__").expanduser().resolve()
        )
        if not (model / "model.bin").is_file() or not importlib.util.find_spec(
            "faster_whisper"
        ):
            raise MediaError("not_configured", "尚未准备离线语音模型和运行依赖。", 503)
        language = cfg.get("language", "zh")
        if language and not re.fullmatch(r"[a-z]{2,3}", language):
            raise MediaError("invalid_config", "语音语言设置无效。", 503)
        if not self._asr_gate.acquire(blocking=False):
            raise MediaError(
                "busy", "离线语音识别正在处理另一段音频，请稍后再试。", 429
            )
        try:
            import av

            try:
                with av.open(str(path)) as audio:
                    duration = audio.duration / av.time_base if audio.duration else None
                    if duration is None or duration <= 0 or duration > 300:
                        raise MediaError(
                            "invalid_audio", "本地转写支持时长已知、5 分钟以内的录音。"
                        )
            except (av.error.FFmpegError, OSError):
                raise MediaError("invalid_audio", "无法读取音频。") from None
            out = _run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("asr_worker.py")),
                    str(model),
                    str(path),
                    language,
                ],
                timeout=180,
            )
            try:
                result = json.loads(out)
                if not isinstance(result, dict) or not isinstance(
                    result.get("text"), str
                ):
                    raise ValueError
                return result
            except ValueError:
                raise MediaError(
                    "invalid_result", "语音识别未返回有效结果。", 502
                ) from None
        finally:
            self._asr_gate.release()

    def _post(self, cfg, suffix, **kwargs):
        if not isinstance(cfg.get("model"), str) or not cfg["model"].strip():
            raise MediaError("invalid_config", "尚未配置媒体模型名称。", 503)
        url, key = _endpoint(cfg, suffix), _key(cfg)
        try:
            response = self.client.post(
                url, headers={"Authorization": "Bearer " + key}, **kwargs
            )
            if response.status_code >= 400:
                raise MediaError(
                    "upstream_error", f"媒体服务返回 HTTP {response.status_code}。", 502
                )
            if len(response.content) > MAX_FILE * 2:
                raise MediaError("invalid_result", "媒体服务响应过大。", 502)
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError
            return value
        except httpx.TimeoutException:
            raise MediaError("timeout", "云端媒体处理超时，未完成。", 504) from None
        except (httpx.HTTPError, ValueError):
            raise MediaError(
                "upstream_error", "媒体服务网络或响应格式异常。", 502
            ) from None

    def _vision(self, data, mime, request, cfg, operation):
        default = (
            "逐行提取图片里的文字。保留数字、时间和否定词；看不清的标为[无法辨认]，不要补写。"
            if operation == "ocr"
            else "用中文描述图片中可见的内容，并区分看见的内容与推测。"
        )
        prompt = request.get("prompt") or default
        response = self._post(
            cfg,
            "/chat/completions",
            json={
                "model": cfg["model"],
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:"
                                    + mime
                                    + ";base64,"
                                    + base64.b64encode(data).decode()
                                },
                            },
                        ],
                    }
                ],
                "max_tokens": 3000,
            },
        )
        try:
            text = response["choices"][0]["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError):
            raise MediaError(
                "invalid_result", "视觉模型没有返回有效文字。", 502
            ) from None
        return {"text": text, "usage": response.get("usage", {})}

    def _remote_asr(self, data, mime, cfg):
        response = self._post(
            cfg,
            "/audio/transcriptions",
            data={"model": cfg["model"]},
            files={"file": ("audio" + MIMES[mime], data, mime)},
        )
        if not isinstance(response.get("text"), str):
            raise MediaError("invalid_result", "转写服务没有返回文字字段。", 502)
        return {"text": response["text"]}

    def _download_image(self, url):
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.port not in {None, 443}
        ):
            raise MediaError("invalid_result", "服务返回了不支持的图片地址。", 502)
        try:
            addresses = socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
            if not addresses or any(
                not ipaddress.ip_address(a[4][0]).is_global for a in addresses
            ):
                raise MediaError("invalid_result", "服务返回了非公网图片地址。", 502)
            # Never forward provider credentials to image storage hosts; no redirects.
            with self.client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise MediaError(
                        "download_failed", "生成完成，但无法下载结果图片。", 502
                    )
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_FILE:
                        raise MediaError("too_large", "生成的图片超过大小限制。", 502)
                    chunks.append(chunk)
                return b"".join(chunks)
        except (httpx.HTTPError, OSError, ValueError):
            raise MediaError(
                "download_failed", "生成完成，但无法下载结果图片。", 502
            ) from None

    def _generate(self, request, cfg):
        prompt = request.get("prompt", "").strip()
        if not prompt:
            raise MediaError("invalid_prompt", "请输入需要生成的图片描述。")
        backend = cfg["backend"]
        if backend == "dashscope_image":
            response = self._post(
                cfg,
                "/api/v1/services/aigc/multimodal-generation/generation",
                json={
                    "model": cfg["model"],
                    "input": {
                        "messages": [{"role": "user", "content": [{"text": prompt}]}]
                    },
                    "parameters": {"size": cfg.get("size", "1024*1024"), "n": 1},
                },
            )
            try:
                content = response["output"]["choices"][0]["message"]["content"]
                url = next(item["image"] for item in content if item.get("image"))
                data = self._download_image(url)
            except (KeyError, IndexError, TypeError, StopIteration):
                raise MediaError(
                    "invalid_result", "生图服务没有返回图片。", 502
                ) from None
        else:
            body = {"model": cfg["model"], "prompt": prompt}
            if backend == "siliconflow_images":
                body["image_size"] = cfg.get("size", "1024x1024")
            else:
                body.update(size=cfg.get("size", "1024x1024"), n=1)
            response = self._post(cfg, "/images/generations", json=body)
            try:
                item = (response.get("images") or response.get("data") or [])[0]
                if item.get("b64_json"):
                    data = base64.b64decode(item["b64_json"], validate=True)
                else:
                    data = self._download_image(item["url"])
            except (KeyError, IndexError, TypeError, ValueError, binascii.Error):
                raise MediaError(
                    "invalid_result", "生图服务没有返回可用图片。", 502
                ) from None
        if len(data) > MAX_FILE:
            raise MediaError("too_large", "生成的图片超过大小限制。", 502)
        mime = _image_check(data)
        return {
            "text": "AI 生成图片",
            "asset": {"mime": mime, "base64": base64.b64encode(data).decode()},
            "usage": response.get("usage", {}),
            "generated": True,
        }
