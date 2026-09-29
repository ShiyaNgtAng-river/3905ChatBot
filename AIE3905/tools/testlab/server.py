"""Local web UI and JSON API for isolated, parallel AstrBot experiments."""

import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal

from aiohttp import web

from runtime import HERE, ROOT, Manager, normalize_dataset, save


def application(manager, token):
    @web.middleware
    async def middleware(request, handler):
        if request.path.startswith("/api/"):
            if request.headers.get("Authorization") != "Bearer " + token:
                return web.json_response(
                    {"error": "请用启动链接打开页面，或填写本地访问令牌"}, status=401
                )
            origin = request.headers.get("Origin")
            if origin and origin != f"http://{request.host}":
                return web.json_response({"error": "拒绝跨站请求"}, status=403)
        try:
            response = await handler(request)
        except web.HTTPException:
            raise
        except (ValueError, KeyError, TypeError) as exc:
            response = web.json_response({"error": str(exc)[:200]}, status=400)
        except Exception as exc:
            response = web.json_response(
                {"error": type(exc).__name__ + ": 请查看该批次日志"}, status=503
            )
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            }
        )
        return response

    app = web.Application(middlewares=[middleware], client_max_size=32 * 1024 * 1024)

    async def static(request):
        name = request.match_info.get("name", "index.html")
        if name not in {"index.html", "app.js", "style.css"}:
            raise web.HTTPNotFound()
        return web.FileResponse(HERE / "ui" / name)

    async def info(request):
        datasets = [
            str(p.parent.relative_to(ROOT))
            for p in (ROOT / "datasets").glob("*/messages.jsonl")
        ]
        return web.json_response(
            {
                "model_mode": manager.model_mode,
                "parallel": manager.parallel,
                "datasets": datasets,
                "host": str(manager.host),
                "home": str(manager.home),
                "differences": [
                    "使用本地虚拟OneBot，不连接QQ",
                    "联网搜索关闭",
                    "关闭分段回复，保留完整文本",
                    "记忆后台按需手动执行或在回放跨天时执行",
                    "每批保留3650天，便于历史数据回放",
                ],
            }
        )

    async def runs(request):
        if request.method == "GET":
            archived = [
                {
                    k: v
                    for k, v in r.items()
                    if k
                    not in {
                        "timeline",
                        "snapshot",
                        "checks",
                        "environment",
                        "metrics",
                        "source_manifest",
                    }
                }
                for r in manager.archived.values()
            ]
            return web.json_response(
                {"runs": archived + [r.info() for r in manager.runs.values()]}
            )
        p = await request.json()
        rows = p.get("rows")
        if p.get("dataset"):
            allowed = {
                str(x.parent.relative_to(ROOT)): x
                for x in (ROOT / "datasets").glob("*/messages.jsonl")
            }
            dataset = allowed.get(p["dataset"])
            if not dataset:
                raise ValueError("请选择已列出的测试集")
            rows = [
                json.loads(line)
                for line in dataset.read_text().splitlines()
                if line.strip()
            ]
        if rows is not None:
            rows = normalize_dataset(rows)
        ids = manager.create(
            p.get("title", "新实验"),
            rows,
            p.get("copies", 1),
            p.get("model_limit", 80),
            bool(p.get("integrate")),
        )
        return web.json_response({"ids": ids}, status=201)

    async def detail(request):
        rid = request.match_info["rid"]
        if rid in manager.runs:
            r = manager.runs[rid]
            if r.session and not r.session.closed and r.state in {"ready", "running"}:
                r.snapshot = await r.api("GET", "/snapshot")
            report = r.report()
        else:
            report = manager.archived[rid]
        return web.json_response(report)

    async def action(request):
        rid, action = request.match_info["rid"], request.match_info["action"]
        r = manager.runs[rid]
        if action == "stop":
            await manager.stop(rid)
            return web.json_response(r.info())
        if r.state != "ready" or r.active_task:
            return web.json_response(
                {"error": "此批次未就绪或仍在处理上一轮"}, status=409
            )
        p = await request.json()
        r.active_task = asyncio.current_task()
        try:
            if action == "step":
                result = await r.step(normalize_dataset([p])[0])
            elif action == "memory":
                result = await r.memory(p["action"], p["day"])
            else:
                raise ValueError("未知操作")
            return web.json_response({"result": result})
        except Exception as exc:
            r.error = type(exc).__name__ + ": " + str(exc)[:140]
            r.persist()
            if isinstance(exc, (TimeoutError, RuntimeError)):
                # A timed-out model can still be running: stop before accepting another turn.
                r.active_task = None
                await manager.stop(rid)
            raise
        finally:
            r.active_task = None

    async def log(request):
        rid = request.match_info["rid"]
        if rid not in manager.runs and rid not in manager.archived:
            raise KeyError("未知批次")
        path = manager.home / rid / "astrbot.log"
        lines = (
            path.read_text(errors="replace").splitlines()[-120:]
            if path.exists()
            else []
        )
        return web.json_response({"text": "\n".join(lines)})

    app.router.add_get("/", static)

    async def health(request):
        return web.json_response({"ok": True})

    app.router.add_get("/health", health)
    app.router.add_get("/api/info", info)
    app.router.add_get("/api/runs", runs)
    app.router.add_post("/api/runs", runs)
    app.router.add_get("/api/runs/{rid}", detail)
    app.router.add_get("/api/runs/{rid}/log", log)
    app.router.add_post("/api/runs/{rid}/{action}", action)
    app.router.add_get("/{name}", static)
    return app


async def main(args):
    os.umask(0o077)
    host = Path(args.astrbot).expanduser().resolve()
    if (
        not (host / "main.py").is_file()
        or not (host / "data/dist/index.html").is_file()
    ):
        raise ValueError("需要已安装且含面板静态文件的 AstrBot")
    home = Path(args.home).resolve()
    if not home.is_relative_to(ROOT / ".sandbox"):
        raise ValueError("测试运行目录必须位于项目 .sandbox 内")
    home.mkdir(parents=True, exist_ok=True)
    lock = open(home / "manager.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    manager = Manager(host, home, args.model, args.parallel)
    token = secrets.token_urlsafe(32)
    runner = web.AppRunner(application(manager, token), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", args.port)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/#token={token}"
    save(home / "access.json", {"url": url, "token": token, "port": port})
    print(
        f"GroupBot Test Lab · {args.model} · parallel={args.parallel}\n{url}",
        flush=True,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in [signal.SIGTERM, signal.SIGINT]:
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await manager.close()
        await runner.cleanup()
        lock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--astrbot", required=True)
    parser.add_argument(
        "--model",
        choices=["host", "mock"],
        default="host",
        help="host 读取宿主模型凭据；mock 不调用外网",
    )
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--parallel", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--home", default=str(ROOT / ".sandbox/testlab"))
    asyncio.run(main(parser.parse_args()))
