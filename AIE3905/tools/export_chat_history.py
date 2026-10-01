"""Export one configured group's recorded text, without changing either database."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3


def export(config_path, host_database, output):
    config_path = Path(config_path).resolve()
    cfg = json.loads(config_path.read_text())
    groups = [
        g for g in cfg["groups"] if g.get("enabled") and g.get("data_use_confirmed")
    ]
    if len(groups) != 1:
        raise ValueError(
            "This exporter requires a configuration with exactly one enabled group"
        )
    g = groups[0]
    key = g["key"]
    database = (config_path.parent / cfg["database"]).resolve()
    with sqlite3.connect("file:" + str(database) + "?mode=ro", uri=True) as c:
        c.row_factory = sqlite3.Row
        c.execute("BEGIN")
        messages = [
            dict(r)
            for r in c.execute(
                "SELECT uid,native_id,sender,name,text,at,reply_to,route,status FROM messages WHERE group_key=? AND erased=0 AND kind!='recall' ORDER BY seq",
                (key,),
            )
        ]
        answers = [
            dict(r)
            for r in c.execute(
                "SELECT id,actor,at,question,output,mode,sources,trace FROM answers WHERE group_key=? ORDER BY at",
                (key,),
            )
        ]
        for answer in answers:
            answer["sources"] = json.loads(answer["sources"])
            answer["trace"] = json.loads(answer["trace"] or "{}")
            answer["delivery_status"] = "not_recorded"
        requests = {}
        for row in c.execute(
            "SELECT message_uid,result FROM dialogue_runs WHERE group_key=?", (key,)
        ):
            result = json.loads(row["result"])
            if result.get("id"):
                requests[result["id"]] = row["message_uid"]
        for answer in answers:
            answer["request_message_uid"] = requests.get(answer["id"])
    host_conversations = []
    if host_database:
        umo = f"{g['platform_id']}:GroupMessage:{g['native_group_id']}"
        with sqlite3.connect(
            "file:" + str(Path(host_database).resolve()) + "?mode=ro", uri=True
        ) as c:
            c.row_factory = sqlite3.Row
            for r in c.execute(
                "SELECT conversation_id,created_at,updated_at,content FROM conversations WHERE platform_id=? AND user_id=? ORDER BY created_at",
                (g["platform_id"], umo),
            ):
                row = dict(r)
                content = json.loads(row.pop("content") or "[]")
                row["messages"] = [
                    {"role": m["role"], "content": m.get("content"), "at": None}
                    for m in content
                    if m.get("role") in {"user", "assistant"}
                ]
                host_conversations.append(row)
    result = {
        "format_version": "1.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "group": key,
        "notes": [
            "这是QQ机器人记录，不是Codex工作对话。",
            "plugin_answers为插件生成记录；host_default_conversations为宿主默认聊天记录，两者不可混为同一回复。",
            "没有平台投递回执的历史记录无法证明送达次数；宿主历史条目无独立时间戳时保留null。",
            "incoming_messages可能包含群成员及其他机器人发言，未按身份猜测类型。",
        ],
        "incoming_messages": messages,
        "plugin_answers": answers,
        "host_default_conversations": host_conversations,
    }
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    os.chmod(output, 0o600)
    return {
        "file": str(output),
        "incoming_messages": len(messages),
        "plugin_answers": len(answers),
        "host_default_messages": sum(len(x["messages"]) for x in host_conversations),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--host-database")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            export(args.config, args.host_database, args.output),
            ensure_ascii=False,
            indent=2,
        )
    )
