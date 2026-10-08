"""Play a fictional parts-order group chat into digital-employee-talk, then ask the bot.

Each member logs in to the web test client with its own cookie jar, the first member
creates the group (or reuses it), the others join, and the script sends the lines
in order. For lines that @ the bot it waits for the bot's reply and prints it with
the time taken. Everything in the script is invented; no real people or prices.

usage: python3 tools/sike_demo_chat.py [--web http://127.0.0.1:3000] [--room room-demo]
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import time
import urllib.error
import urllib.request

SCRIPT = [
    ("王师傅", "小李在吗，奥迪A4L 2019款前刹车片要一套，有货没？"),
    ("小李", "在的王师傅，原厂和博世两种都有货，原厂680一套，博世420一套"),
    ("王师傅", "客户嫌贵，博世吧"),
    ("小李", "好，博世今天下午发，明天上午到"),
    ("王师傅", "能今天到吗？车主下午五点就要提车"),
    ("小李", "我问问仓库"),
    ("陈经理", "走同城闪送应该可以吧？"),
    ("小李", "仓库说下午单子排满了，闪送也来不及，最快明早9点前到"),
    ("王师傅", "行吧，那就明早9点，我跟车主说一声"),
    ("王师傅", "对了，再加一个机油滤芯，06L115562"),
    ("小李", "这个型号缺货，周四才能到"),
    ("陈经理", "滤芯先别发，问问王师傅要不要换曼牌的，曼牌有现货"),
    ("小李", "@王师傅 曼牌的滤芯有现货，45一个，要换吗？"),
    ("陈经理", "@数字员工 王师傅这单现在什么情况？刹车片哪天到，滤芯定了没？"),
    ("王师傅", "@数字员工 刚才说的刹车片多少钱来着？博世那个"),
    ("王师傅", "曼牌可以，换吧"),
    ("小李", "好，曼牌滤芯跟刹车片一起明早到"),
    ("陈经理", "@数字员工 帮我把王师傅这单整理成一段话，我转给仓库"),
]


class Member:
    def __init__(self, web, name):
        self.web, self.name = web, name
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self.call("POST", "/api/session", {"username": name})

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.web + path, data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        with self.opener.open(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--web", default="http://127.0.0.1:3000")
    p.add_argument("--room", default="room-demo")
    p.add_argument("--name", default="汽配订货群（演示）")
    p.add_argument("--gap", type=float, default=1.5, help="seconds between ordinary lines")
    p.add_argument("--wait", type=float, default=240, help="max seconds to wait for a reply")
    a = p.parse_args()
    members = {}
    for name, _ in SCRIPT:
        if name not in members:
            members[name] = Member(a.web, name)
    first = members[SCRIPT[0][0]]
    try:
        group = first.call("POST", "/api/chat/groups", {"name": a.name, "roomId": a.room})
    except urllib.error.HTTPError as exc:
        if exc.code != 409:
            raise
        group = first.call("POST", f"/api/chat/groups/{a.room}/join")
    conv = group["conversation"]["id"]
    for m in members.values():
        m.call("POST", f"/api/chat/groups/{a.room}/join")
    print(f"group {a.room} ready; members: {', '.join(members)}", flush=True)
    for name, text in SCRIPT:
        m = members[name]
        before = {x["id"] for x in m.call("GET", f"/api/chat/conversations/{conv}/messages")["messages"]}
        m.call("POST", f"/api/chat/conversations/{conv}/messages", {"content": text})
        print(f"{name}: {text}", flush=True)
        if "@数字员工" not in text:
            time.sleep(a.gap)
            continue
        started = time.monotonic()
        reply = None
        while time.monotonic() - started < a.wait and reply is None:
            time.sleep(1)
            for x in m.call("GET", f"/api/chat/conversations/{conv}/messages")["messages"]:
                if x["role"] == "bot" and x["id"] not in before:
                    reply = x
        if reply is None:
            print(f"  (no reply within {a.wait:.0f}s)", flush=True)
        else:
            print(f"数字员工（{time.monotonic() - started:.1f}s）: {reply['content']}", flush=True)
        time.sleep(a.gap)


if __name__ == "__main__":
    main()
