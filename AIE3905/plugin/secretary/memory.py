from __future__ import annotations

import re
from datetime import date, datetime

from .types import EVENT_KINDS, FIELDS, Group, digest

PROMPT_VERSION = "events-1.0"


def validate_candidates(store, group: Group, m: dict, candidates: list, model: str) -> list[dict]:
    if not isinstance(candidates, list) or len(candidates) > 6:
        raise ValueError("事件列表无效或超过每条消息 6 个事件的限制")
    known = {e["id"]: e for e in store.events(group.key, m["at"]) if e["valid"]}
    out = []
    for index, c in enumerate(candidates):
        if not isinstance(c, dict) or c.get("kind") not in EVENT_KINDS:
            raise ValueError("事件类型无效")
        title = str(c.get("title", "")).strip()
        if not title or len(title) > 100:
            raise ValueError("事项标题应为 1–100 字")
        iid = digest(group.key, title.casefold())[:20]
        item = store.one("SELECT * FROM items WHERE group_key=? AND id=?", (group.key,iid))
        creator = item["creator"] if item else m["sender"]
        payload = dict(c.get("fields", {}))
        if set(payload) - FIELDS:
            raise ValueError("包含未允许的字段")
        if any(not isinstance(v, (str,int,bool)) or len(str(v)) > 1000 for v in payload.values()):
            raise ValueError("字段值类型或长度无效")
        if "priority" in payload and (type(payload["priority"]) is not int or not 1 <= payload["priority"] <= 5):
            raise ValueError("priority 必须为 1–5 的整数")
        if "target_count" in payload and (type(payload["target_count"]) is not int or not 0 <= payload["target_count"] <= 10000):
            raise ValueError("人数无效")
        if payload.get("when"):
            value = str(payload["when"])
            parsed = datetime.fromisoformat(value)
            if len(value) > 10 and parsed.tzinfo is None:
                raise ValueError("具体时间必须包含时区")
        scope, occurrence = c.get("scope", "series"), c.get("occurrence", "")
        if scope not in {"series", "occurrence"}:
            raise ValueError("scope 无效")
        if scope == "occurrence":
            date.fromisoformat(occurrence)
        target = str(c.get("target", ""))
        sources = list(dict.fromkeys([m["uid"]] + c.get("sources", [])))
        if len(sources) > 20 or not all(isinstance(s,str) for s in sources):
            raise ValueError("证据列表无效")
        if target:
            prev = known.get(target)
            if not prev or prev["item_id"] != iid:
                raise ValueError("目标事件不存在或不属于该事项")
            if c["kind"] == "correct" and prev["actor"] != m["sender"] and m["sender"] not in group.admins:
                raise PermissionError("只能更正自己的陈述，管理员除外")
            if c["kind"] == "confirm" and not payload:
                payload = dict(prev["payload"])
                scope, occurrence = prev["scope"], prev["occurrence"]
                sources = list(dict.fromkeys(sources + prev["sources"]))
            if c['kind']=='correct':
                inherited={k:v for k,v in prev['payload'].items() if k not in payload}
                if 'time_raw' in payload:
                    inherited.pop('when',None)
                if inherited:
                    payload={**inherited,**payload}
                    sources=list(dict.fromkeys(sources+prev['sources']))
                scope,occurrence=prev['scope'],prev['occurrence']
        if len(sources)>40:
            raise ValueError('事件依赖过多，需要独立重述确认')
        for sid in sources:
            source = store.message(group.key, sid)
            if not source or source["at"] > m["at"]:
                raise ValueError("证据缺失、被撤回、跨群或来自未来")
        kind = c["kind"]
        if kind == 'confirm' and target and known[target]['kind'] in {'cancel','complete','change','outdated'}:
            kind = known[target]['kind']
        allowed = group.can_confirm(m["sender"], creator)
        accepted = kind == "note" or (kind != "propose" and allowed)
        reason = "" if accepted else "awaiting_confirmation"
        if kind == "correct" and target:
            # Self-correction of a proposal does not promote it to an official decision.
            accepted = bool(known[target]["accepted"] and (allowed or known[target]["actor"] == m["sender"]))
        if kind == "participant":
            participant = str(c.get("participant", m["sender"]))
            if participant != m["sender"] and not allowed:
                raise PermissionError("不能替其他成员修改状态")
            state = c.get("participant_status", "unknown")
            if state not in {"attending","absent","responsible","blocked","unknown"}:
                raise ValueError("成员状态无效")
            payload = {"person": participant, "status": state, **payload}
            accepted, reason = True, ""
        out.append(dict(id=digest(m["uid"],index)[:24],group_key=group.key,item_id=iid,
            title=title,creator=creator,message_uid=m["uid"],actor=m["sender"],at=m["at"],
            kind=kind,payload=payload,sources=sources,target=target,scope=scope,occurrence=occurrence,
            accepted=int(accepted),reason=reason,model=model,prompt_version=PROMPT_VERSION))
    return out


def project(events: list[dict]) -> list[dict]:
    states = {}
    valid = [e for e in events if e["valid"]]
    replaced = {e["target"] for e in valid if e["kind"] == "correct" and e["target"]}
    confirmed = {e["target"] for e in valid if e['target'] and e["accepted"]}
    for e in events:
        s = states.setdefault(e["item_id"], {"id":e["item_id"],"title":e["title"],"creator":e["creator"],
            "status":"unconfirmed","fields":{},"field_sources":{},"participants":{},"occurrences":{},
            "pending":[],"history":[],"last_update":e["at"],"priority":1})
        if e['valid']:
            s["history"].append(e)
        s["last_update"] = max(s["last_update"], e["at"])
        if not e['valid']:
            # A withdrawn change does not assert that the old arrangement is back in force.
            if e['accepted'] and e['kind'] not in {'participant','note'}:
                dest = s if e['scope'] == 'series' else s['occurrences'].setdefault(e['occurrence'], {'fields':{},'field_sources':{},'participants':{}})
                dest.update(status='uncertain', fields={}, field_sources={}, status_sources=[])
            continue
        if e["id"] in replaced:
            continue
        if not e["accepted"]:
            if e["id"] not in confirmed:
                s["pending"].append(e)
            continue
        dest = s
        if e["scope"] == "occurrence":
            dest = s["occurrences"].setdefault(e["occurrence"], {"status":"unconfirmed","fields":{},"field_sources":{},"participants":{}})
        if e["kind"] == "participant":
            p = e["payload"]
            dest["participants"][p["person"]] = {"status":p["status"],"sources":e["sources"]}
        elif e["kind"] == "note":
            dest["fields"]["note"] = e["payload"].get("note", "")
            dest["field_sources"]["note"] = e["sources"]
        elif e['kind']=='outdated':
            dest.update(status='uncertain', fields=dict(e['payload']),field_sources={k:e['sources'] for k in e['payload']},status_sources=e['sources'])
        else:
            dest["status"] = {"cancel":"cancelled","complete":"completed"}.get(e["kind"],"confirmed")
            dest["status_sources"] = e["sources"]
            if 'time_raw' in e['payload'] and 'when' not in e['payload']:
                dest['fields'].pop('when',None)
                dest['field_sources'].pop('when',None)
            for k,v in e["payload"].items():
                dest["fields"][k] = v
                dest["field_sources"][k] = e["sources"]
        if 'priority' in e['payload']:
            s['priority'] = int(e['payload']['priority'])
    return list(states.values())


def terms(text):
    text = text.casefold()
    out = re.findall(r"[a-z0-9_]+", text)
    for part in re.findall(r"[\u4e00-\u9fff]+", text):
        out.extend(part[i:i+2] for i in range(len(part)-1))
        out.append(part)
    return set(out)


def rank(query, records, text_getter):
    q = terms(query)
    scored = []
    for r in records:
        text = text_getter(r)
        overlap = len(q & terms(text))
        if overlap:
            scored.append((overlap / max(1,len(q)),r))
    return [r for _,r in sorted(scored,key=lambda p:p[0],reverse=True)]
