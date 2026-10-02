from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .types import Message, digest, utcnow
from .memory import terms


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None, timeout=10
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        PRAGMA secure_delete=ON;
        CREATE TABLE IF NOT EXISTS messages(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT UNIQUE NOT NULL, group_key TEXT NOT NULL,
          native_id TEXT, sender TEXT NOT NULL, name TEXT, text TEXT, at TEXT NOT NULL,
          kind TEXT, revision TEXT, target_id TEXT, reply_to TEXT, source_url TEXT, attachments TEXT,
          fingerprint TEXT, dataset TEXT, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
          error TEXT DEFAULT '', erased INTEGER DEFAULT 0, duplicate_of TEXT DEFAULT '');
        CREATE INDEX IF NOT EXISTS message_group ON messages(group_key,seq);
        CREATE INDEX IF NOT EXISTS message_native ON messages(group_key,native_id);
        CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, group_key TEXT, title TEXT, creator TEXT);
        CREATE TABLE IF NOT EXISTS events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, group_key TEXT, item_id TEXT,
          message_uid TEXT, actor TEXT, at TEXT, kind TEXT, payload TEXT, sources TEXT,
          target TEXT, scope TEXT, occurrence TEXT, accepted INTEGER, valid INTEGER DEFAULT 1,
          reason TEXT, model TEXT, prompt_version TEXT);
        CREATE INDEX IF NOT EXISTS event_group ON events(group_key,item_id);
        CREATE TABLE IF NOT EXISTS preferences(group_key TEXT, sender TEXT, opted_out INTEGER,
          PRIMARY KEY(group_key,sender));
        CREATE TABLE IF NOT EXISTS tombstones(group_key TEXT, token TEXT, PRIMARY KEY(group_key,token));
        CREATE TABLE IF NOT EXISTS answers(id TEXT PRIMARY KEY, group_key TEXT, actor TEXT, at TEXT,
          question TEXT, output TEXT, sources TEXT, item_ids TEXT, mode TEXT);
        CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY, group_key TEXT, actor TEXT,
          answer_id TEXT, kind TEXT, note TEXT, at TEXT);
        CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY, group_key TEXT, role TEXT,
          model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, seconds REAL, error TEXT, at TEXT);
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,group_key TEXT,actor TEXT,action TEXT,
          target_hash TEXT,at TEXT);
        PRAGMA user_version=1;
        """)
        if "trace" not in {
            r["name"] for r in self.conn.execute("PRAGMA table_info(answers)")
        }:
            self.conn.execute("ALTER TABLE answers ADD COLUMN trace TEXT DEFAULT '{}'")
        # Additive migrations preserve historical messages and their processing status.
        if "route" not in {
            r["name"] for r in self.conn.execute("PRAGMA table_info(messages)")
        }:
            self.conn.execute(
                "ALTER TABLE messages ADD COLUMN route TEXT DEFAULT 'background'"
            )
        if "provenance" not in {
            r["name"] for r in self.conn.execute("PRAGMA table_info(events)")
        }:
            self.conn.execute(
                "ALTER TABLE events ADD COLUMN provenance TEXT DEFAULT '{}'"
            )
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS drafts(
          id TEXT PRIMARY KEY, group_key TEXT, actor TEXT, message_uid TEXT, answer_id TEXT,
          option_number INTEGER, title TEXT, description TEXT, fields TEXT, sources TEXT,
          family TEXT, version INTEGER, at TEXT, active INTEGER DEFAULT 1);
        CREATE INDEX IF NOT EXISTS draft_group ON drafts(group_key,actor,at);
        CREATE TABLE IF NOT EXISTS dialogue_runs(
          message_uid TEXT PRIMARY KEY, group_key TEXT, actor TEXT, result TEXT, at TEXT);
        PRAGMA user_version=2;
        """)
        # v3: derived long-range memory. Every row lists the message uids it was
        # generated from, so removing any source removes the derived text too.
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS episodes(
          id TEXT PRIMARY KEY, group_key TEXT, start_seq INTEGER, end_seq INTEGER,
          start_at TEXT, end_at TEXT, participants TEXT, summary TEXT, topics TEXT,
          sources TEXT, at TEXT);
        CREATE INDEX IF NOT EXISTS episode_group ON episodes(group_key,end_seq);
        CREATE TABLE IF NOT EXISTS profiles(
          group_key TEXT, sender TEXT, name TEXT, summary TEXT, episodes TEXT,
          updated_at TEXT, PRIMARY KEY(group_key,sender));
        PRAGMA user_version=3;
        """)
        # v4: whole-day reading and long-term anchors. Derived rows cite message
        # seqs ("m" lists) so a removed message takes its derived text with it.
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS day_views(
          group_key TEXT, day TEXT, sidebar TEXT, first_seq INTEGER, last_seq INTEGER,
          passes INTEGER DEFAULT 0, updated_at TEXT, PRIMARY KEY(group_key,day));
        CREATE TABLE IF NOT EXISTS anchor_topics(
          id INTEGER PRIMARY KEY AUTOINCREMENT, group_key TEXT, title TEXT, aliases TEXT,
          status TEXT, importance INTEGER, first_day TEXT, last_day TEXT, days_seen INTEGER,
          sources TEXT, updated_at TEXT);
        CREATE INDEX IF NOT EXISTS anchor_topic_group ON anchor_topics(group_key,last_day);
        CREATE TABLE IF NOT EXISTS anchor_facts(
          id INTEGER PRIMARY KEY AUTOINCREMENT, group_key TEXT, topic_id INTEGER, kind TEXT,
          statement TEXT, day TEXT, invalid_day TEXT DEFAULT '', superseded_by INTEGER DEFAULT 0,
          uncertain INTEGER DEFAULT 0, sources TEXT, at TEXT);
        CREATE INDEX IF NOT EXISTS anchor_fact_topic ON anchor_facts(group_key,topic_id);
        CREATE TABLE IF NOT EXISTS digests(
          group_key TEXT, level TEXT, period TEXT, qa TEXT, sources TEXT, at TEXT,
          PRIMARY KEY(group_key,level,period));
        CREATE TABLE IF NOT EXISTS lexicon(
          group_key TEXT, term TEXT, meaning TEXT, sources TEXT, updated_at TEXT,
          PRIMARY KEY(group_key,term));
        PRAGMA user_version=4;
        """)
        if "sources" not in {
            r["name"] for r in self.conn.execute("PRAGMA table_info(profiles)")
        }:
            self.conn.execute("ALTER TABLE profiles ADD COLUMN sources TEXT DEFAULT '[]'")
        if "cached_tokens" not in {
            r["name"] for r in self.conn.execute("PRAGMA table_info(usage)")
        }:
            self.conn.execute("ALTER TABLE usage ADD COLUMN cached_tokens INTEGER")
        # v5: what the bot learned from how people reacted to it. "about" is a sender,
        # or '' for the whole group; sources are message seqs like the v4 tables.
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS lessons(
          id INTEGER PRIMARY KEY AUTOINCREMENT, group_key TEXT, about TEXT, text TEXT,
          sources TEXT, first_day TEXT, last_day TEXT, updated_at TEXT);
        CREATE INDEX IF NOT EXISTS lesson_group ON lessons(group_key,about);
        PRAGMA user_version=5;
        """)
        self.fts = True
        try:
            existing = self.one("SELECT 1 FROM sqlite_master WHERE name='message_fts'")
            self.conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(uid UNINDEXED,group_key UNINDEXED,tokens)"
            )
            if not existing:
                with self.tx() as db:
                    db.executemany(
                        "INSERT INTO message_fts(uid,group_key,tokens) VALUES(?,?,?)",
                        [
                            (
                                r["uid"],
                                r["group_key"],
                                " ".join(sorted(terms(r["text"]))),
                            )
                            for r in self.rows(
                                "SELECT uid,group_key,text FROM messages WHERE erased=0 AND kind!='recall'"
                            )
                        ],
                    )
        except sqlite3.OperationalError:
            self.fts = False
        os.chmod(path, 0o600)

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def get_meta(self, key, default=""):
        r = self.one("SELECT value FROM metadata WHERE key=?", (key,))
        return r["value"] if r else default

    def set_meta(self, key, value):
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, str(value)))

    def opted_out(self, group, sender):
        r = self.one(
            "SELECT opted_out FROM preferences WHERE group_key=? AND sender=?",
            (group, sender),
        )
        return bool(r and r["opted_out"])

    def put(self, m: Message, route="background"):
        with self.tx() as db:
            if m.kind != "recall" and self.opted_out(m.group, m.sender):
                return None
            tokens = ["uid:" + m.uid, "fp:" + m.fingerprint]
            if m.native_id:
                tokens.append("native:" + digest(m.native_id))
            if any(
                self.one(
                    "SELECT 1 FROM tombstones WHERE group_key=? AND token=?",
                    (m.group, t),
                )
                for t in tokens
            ):
                return None
            old = self.one("SELECT * FROM messages WHERE uid=?", (m.uid,))
            if old:
                if not old["erased"] and old["fingerprint"] != m.fingerprint:
                    raise ValueError(
                        "相同消息 ID 对应不同内容；编辑消息须使用新的 revision 和 edit 类型"
                    )
                return dict(old, _new=False)
            duplicate = self.one(
                "SELECT uid FROM messages WHERE group_key=? AND fingerprint=? AND erased=0 LIMIT 1",
                (m.group, m.fingerprint),
            )
            db.execute(
                """INSERT INTO messages(uid,group_key,native_id,sender,name,text,at,kind,revision,
              target_id,reply_to,source_url,attachments,fingerprint,dataset,duplicate_of)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    m.uid,
                    m.group,
                    m.native_id,
                    m.sender,
                    m.name,
                    m.text,
                    m.at,
                    m.kind,
                    m.revision,
                    m.target_id,
                    m.reply_to,
                    m.source_url,
                    encode(m.attachments),
                    m.fingerprint,
                    m.dataset,
                    duplicate["uid"] if duplicate and not m.native_id else "",
                ),
            )
            db.execute(
                "UPDATE messages SET route=?,status=? WHERE uid=?",
                (route, "dialogue" if route == "dialogue" else "pending", m.uid),
            )
            if self.fts and m.kind != "recall":
                db.execute(
                    "INSERT INTO message_fts(uid,group_key,tokens) VALUES(?,?,?)",
                    (m.uid, m.group, " ".join(sorted(terms(m.text)))),
                )
            return dict(
                self.one("SELECT * FROM messages WHERE uid=?", (m.uid,)), _new=True
            )

    def pending(self, group, maximum=3):
        # Failed records stay observable; they do not create a false contiguous high-water mark.
        return self.one(
            """SELECT * FROM messages WHERE group_key=? AND erased=0
          AND status IN ('pending','retry') AND attempts<? ORDER BY seq LIMIT 1""",
            (group, maximum),
        )

    def failure(self, uid, error, maximum):
        with self.tx() as db:
            db.execute(
                """UPDATE messages SET attempts=attempts+1, error=?,
              status=CASE WHEN attempts+1>=? THEN 'failed' ELSE 'retry' END WHERE uid=? AND erased=0""",
                (error, maximum, uid),
            )

    def message(self, group, uid_or_native):
        return self.one(
            """SELECT * FROM messages WHERE group_key=? AND (uid=? OR native_id=?)
           AND erased=0 AND kind!='recall' ORDER BY seq DESC LIMIT 1""",
            (group, uid_or_native, uid_or_native),
        )

    def recent(self, group, before=None, limit=30):
        where, args = "group_key=? AND erased=0 AND kind!='recall'", [group]
        if before:
            where += " AND at<=?"
            args.append(before)
        args.append(limit)
        rows = self.rows(
            f"SELECT * FROM messages WHERE {where} ORDER BY seq DESC LIMIT ?", args
        )
        return list(reversed(rows))

    def events(self, group, before=None):
        sql, args = (
            "SELECT e.*,i.title,i.creator FROM events e JOIN items i ON i.id=e.item_id WHERE e.group_key=?",
            [group],
        )
        if before:
            sql += " AND e.at<=?"
            args.append(before)
        rows = self.rows(sql + " ORDER BY e.at,e.seq", args)
        for r in rows:
            r["payload"], r["sources"] = (
                json.loads(r["payload"]),
                json.loads(r["sources"]),
            )
            r["provenance"] = json.loads(r.get("provenance") or "{}")
        return rows

    def search(
        self,
        group,
        query,
        before=None,
        limit=12,
        exclude_uid="",
        since=None,
        senders=None,
    ):
        """Find messages by words, optionally within a time range and by senders.

        Args:
            group: Group key.
            query: Words to match; when empty, filters alone select the newest rows.
            before: Upper time bound (inclusive), defaults to now.
            limit: Maximum rows, 1–100.
            exclude_uid: Message to leave out, usually the current request.
            since: Lower time bound (inclusive), or None.
            senders: Sender ids to keep, or None for everyone.

        Returns:
            Message rows, best match first; newest first when query is empty.
        """
        tokens = sorted(terms(query))[:64]
        limit = max(1, min(int(limit), 100))
        extra, args = "", []
        if since:
            extra += " AND m.at>=?"
            args.append(since)
        if senders is not None:
            if not senders:
                return []
            extra += f" AND m.sender IN ({','.join('?' for _ in senders)})"
            args += list(senders)
        base = [group, before or utcnow(), exclude_uid]
        if not tokens:
            if not since and senders is None:
                return []
            return self.rows(
                f"""SELECT m.* FROM messages m WHERE m.group_key=? AND m.erased=0 AND m.kind!='recall'
                AND m.at<=? AND m.uid!=?{extra} ORDER BY m.seq DESC LIMIT ?""",
                [*base, *args, limit],
            )
        if self.fts:
            match = " OR ".join(
                '"' + token.replace('"', '""') + '"' for token in tokens
            )
            return self.rows(
                f"""SELECT m.* FROM message_fts JOIN messages m ON m.uid=message_fts.uid
                WHERE message_fts MATCH ? AND m.group_key=? AND m.erased=0 AND m.kind!='recall'
                AND m.at<=? AND m.uid!=?{extra} ORDER BY bm25(message_fts),m.at DESC LIMIT ?""",
                [match, *base, *args, limit],
            )
        clauses = " OR ".join("instr(lower(m.text),?)>0" for _ in tokens)
        return self.rows(
            f"""SELECT m.* FROM messages m WHERE m.group_key=? AND m.erased=0 AND m.kind!='recall'
            AND m.at<=? AND m.uid!=?{extra} AND ({clauses}) ORDER BY m.at DESC LIMIT ?""",
            [*base, *args, *tokens, limit],
        )

    def drafts(self, group, actor=None, before=None):
        sql = "SELECT * FROM drafts WHERE group_key=? AND active=1"
        args = [group]
        if actor is not None:
            sql += " AND actor=?"
            args.append(actor)
        if before:
            sql += " AND at<=?"
            args.append(before)
        rows = self.rows(sql + " ORDER BY at DESC,rowid DESC LIMIT 24", args)
        for row in rows:
            row["fields"], row["sources"] = (
                json.loads(row["fields"]),
                json.loads(row["sources"]),
            )
        return rows

    def resolve_draft(self, group, message, draft_id):
        drafts = self.drafts(group, before=message["at"])
        selected = next((d for d in drafts if d["id"] == draft_id), None)
        if not selected:
            raise ValueError("草案不存在、已被修改或已过期")
        reply = message.get("reply_to", "")
        explicitly_referenced = reply and reply in {
            selected["answer_id"],
            selected["id"],
        }
        if not explicitly_referenced:
            # Without an explicit quote only use this member's most recent draft set.
            own = [d for d in drafts if d["actor"] == message["sender"]]
            if (
                not own
                or selected["actor"] != message["sender"]
                or selected["answer_id"] != own[0]["answer_id"]
            ):
                raise ValueError("请明确引用要采用的方案，或重新说明安排")
        if any(not self.message(group, sid) for sid in selected["sources"]):
            raise ValueError("草案依据已被删除")
        return selected

    def commit(self, message: dict, events: list[dict], status="done"):
        with self.tx() as db:
            current = self.one("SELECT * FROM messages WHERE uid=?", (message["uid"],))
            if not current or current["erased"] or current["status"] == "done":
                return
            for e in events:
                # Re-check evidence after the model call: revocation can happen while awaiting it.
                sources = [
                    self.message(message["group_key"], sid) for sid in e["sources"]
                ]
                if not sources or any(
                    not s or s["at"] > message["at"] for s in sources
                ):
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO items VALUES(?,?,?,?)",
                    (e["item_id"], e["group_key"], e["title"], e["creator"]),
                )
                db.execute(
                    "UPDATE items SET title=? WHERE id=? AND title='[证据已移除]'",
                    (e["title"], e["item_id"]),
                )
                keys = [
                    "id",
                    "group_key",
                    "item_id",
                    "message_uid",
                    "actor",
                    "at",
                    "kind",
                    "payload",
                    "sources",
                    "target",
                    "scope",
                    "occurrence",
                    "accepted",
                    "reason",
                    "model",
                    "prompt_version",
                    "provenance",
                ]
                values = [
                    encode(e.get(k, {}))
                    if k in {"payload", "sources", "provenance"}
                    else e[k]
                    for k in keys
                ]
                db.execute(
                    f"INSERT OR IGNORE INTO events({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
                    values,
                )
            db.execute(
                "UPDATE messages SET status=?,error='' WHERE uid=?",
                (status, message["uid"]),
            )
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES(?,?)",
                ("last_processed:" + message["group_key"], str(message["seq"])),
            )

    def _purge(self, db, group, ids, actor, action, freeze_native=True):
        ids = set(ids)
        seqs = set()
        for uid in ids:
            m = self.one(
                "SELECT * FROM messages WHERE group_key=? AND uid=?", (group, uid)
            )
            if not m:
                continue
            seqs.add(m["seq"])
            for token in ["uid:" + uid, "fp:" + m["fingerprint"]] + (
                ["native:" + digest(m["native_id"])]
                if m["native_id"] and freeze_native
                else []
            ):
                db.execute(
                    "INSERT OR IGNORE INTO tombstones VALUES(?,?)", (group, token)
                )
            db.execute(
                "UPDATE messages SET erased=1,text='',name='',sender='',attachments='[]',source_url='',error='',status='done' WHERE uid=?",
                (uid,),
            )
            if self.fts:
                db.execute("DELETE FROM message_fts WHERE uid=?", (uid,))
        for e in self.events(group):
            if ids.intersection(e["sources"]):
                db.execute(
                    "UPDATE events SET valid=0,payload='{}',provenance='{}',actor='',reason='evidence_removed' WHERE id=?",
                    (e["id"],),
                )
        # Episodes summarise their sources; member notes are rebuilt from episodes.
        gone = {
            r["id"]
            for r in self.rows(
                "SELECT id,sources FROM episodes WHERE group_key=?", (group,)
            )
            if ids.intersection(json.loads(r["sources"]))
        }
        for eid in gone:
            db.execute("DELETE FROM episodes WHERE id=?", (eid,))
        for p in self.rows(
            "SELECT sender,episodes FROM profiles WHERE group_key=?", (group,)
        ):
            if gone.intersection(json.loads(p["episodes"])):
                db.execute(
                    "DELETE FROM profiles WHERE group_key=? AND sender=?",
                    (group, p["sender"]),
                )
        if seqs:
            self._purge_derived(db, group, seqs, action)
        if action == "retention":
            # Expiry is routine: drop only replies that used an expired message. Older
            # replies go by date in expire(); wiping all of them would leave nothing to
            # learn from, since some message expires on almost every tick.
            for a in self.rows(
                "SELECT id,sources FROM answers WHERE group_key=?", (group,)
            ):
                if ids.intersection(json.loads(a["sources"] or "[]")):
                    db.execute("DELETE FROM answers WHERE id=?", (a["id"],))
                    db.execute("DELETE FROM feedback WHERE answer_id=?", (a["id"],))
        else:
            # Remove generated text conservatively: it may paraphrase removed evidence.
            db.execute("DELETE FROM answers WHERE group_key=?", (group,))
            db.execute("DELETE FROM feedback WHERE group_key=?", (group,))
        db.execute("DELETE FROM drafts WHERE group_key=?", (group,))
        db.execute("DELETE FROM dialogue_runs WHERE group_key=?", (group,))
        db.execute(
            "UPDATE items SET title='[证据已移除]' WHERE group_key=? AND id NOT IN (SELECT item_id FROM events WHERE valid=1)",
            (group,),
        )
        db.execute(
            "INSERT INTO audit(group_key,actor,action,target_hash,at) VALUES(?,?,?,?,?)",
            (group, actor, action, digest(sorted(ids)), utcnow()),
        )
        db.execute(
            "INSERT OR REPLACE INTO metadata VALUES(?,?)",
            ("revocation:" + group, utcnow()),
        )

    def _purge_derived(self, db, group, seqs, action=""):
        """Drop every v4 derived entry that cites one of the removed message seqs.

        Day views and daily digests lose only the points and answers citing them;
        week and month digests are deleted and rebuilt from the daily ones. A fact
        that superseded an older one hands the topic back to that older fact,
        marked uncertain. The group portrait (people, terms, lessons) is distilled
        again and again: on expiry it only loses the expired seqs and goes when none
        are left; a recall, edit or opt-out removes the whole entry.
        """

        def hit(value):
            return bool(seqs.intersection(value if isinstance(value, list) else []))

        lo, hi = min(seqs), max(seqs)
        for v in self.rows(
            "SELECT day,sidebar FROM day_views WHERE group_key=? AND first_seq<=? AND last_seq>=?",
            (group, hi, lo),
        ):
            side = json.loads(v["sidebar"])
            topics = []
            for t in side.get("topics", []):
                points = [p for p in t.get("points", []) if not hit(p.get("m"))]
                if points:
                    topics.append(dict(t, points=points))
            side["topics"] = topics
            for part in ("terms", "feedback"):
                side[part] = [x for x in side.get(part, []) if not hit(x.get("m"))]
            db.execute(
                "UPDATE day_views SET sidebar=? WHERE group_key=? AND day=?",
                (encode(side), group, v["day"]),
            )
        for d in self.rows(
            "SELECT level,period,qa,sources FROM digests WHERE group_key=?", (group,)
        ):
            if not hit(json.loads(d["sources"])):
                continue
            qa = [a for a in json.loads(d["qa"]) if not hit(a.get("m"))]
            # A daily row stays, even empty, as the marker that the day was consolidated.
            if d["level"] != "day":
                db.execute(
                    "DELETE FROM digests WHERE group_key=? AND level=? AND period=?",
                    (group, d["level"], d["period"]),
                )
                continue
            db.execute(
                "UPDATE digests SET qa=?,sources=? WHERE group_key=? AND level=? AND period=?",
                (
                    encode(qa),
                    encode(sorted({n for a in qa for n in a.get("m", [])})),
                    group,
                    d["level"],
                    d["period"],
                ),
            )
        for f in self.rows(
            "SELECT id,sources FROM anchor_facts WHERE group_key=?", (group,)
        ):
            if hit(json.loads(f["sources"])):
                db.execute("DELETE FROM anchor_facts WHERE id=?", (f["id"],))
                db.execute(
                    "UPDATE anchor_facts SET superseded_by=0,invalid_day='',uncertain=1 WHERE superseded_by=?",
                    (f["id"],),
                )
        for t in self.rows(
            "SELECT id,sources FROM anchor_topics WHERE group_key=?", (group,)
        ):
            sources = json.loads(t["sources"])
            if not hit(sources):
                continue
            if self.one("SELECT 1 FROM anchor_facts WHERE topic_id=?", (t["id"],)):
                db.execute(
                    "UPDATE anchor_topics SET sources=? WHERE id=?",
                    (encode([n for n in sources if n not in seqs]), t["id"]),
                )
            else:
                db.execute("DELETE FROM anchor_topics WHERE id=?", (t["id"],))
        for table, key in (("lexicon", "term"), ("profiles", "sender"), ("lessons", "id")):
            for r in self.rows(
                f"SELECT {key},sources FROM {table} WHERE group_key=?", (group,)
            ):
                sources = json.loads(r["sources"] or "[]")
                if not hit(sources):
                    continue
                left = [n for n in sources if n not in seqs]
                if action == "retention" and left:
                    db.execute(
                        f"UPDATE {table} SET sources=? WHERE group_key=? AND {key}=?",
                        (encode(left), group, r[key]),
                    )
                else:
                    db.execute(
                        f"DELETE FROM {table} WHERE group_key=? AND {key}=?",
                        (group, r[key]),
                    )

    def supersede_revision(self, group, native_id, new_uid):
        with self.tx() as db:
            ids = [
                r["uid"]
                for r in self.rows(
                    "SELECT uid FROM messages WHERE group_key=? AND native_id=? AND uid!=? AND erased=0",
                    (group, native_id, new_uid),
                )
            ]
            if ids:
                self._purge(db, group, ids, "platform", "edit", freeze_native=False)

    def recall(self, group, native_id, actor="platform"):
        with self.tx() as db:
            ids = [
                r["uid"]
                for r in self.rows(
                    "SELECT uid FROM messages WHERE group_key=? AND native_id=?",
                    (group, native_id),
                )
            ]
            db.execute(
                "INSERT OR IGNORE INTO tombstones VALUES(?,?)",
                (group, "native:" + digest(native_id)),
            )
            self._purge(db, group, ids, actor, "recall")

    def optout(self, group, sender, enabled=True):
        with self.tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO preferences VALUES(?,?,?)",
                (group, sender, int(enabled)),
            )
            if enabled:
                ids = [
                    r["uid"]
                    for r in self.rows(
                        "SELECT uid FROM messages WHERE group_key=? AND sender=?",
                        (group, sender),
                    )
                ]
                self._purge(db, group, ids, sender, "opt_out")
                for table, column in (("profiles", "sender"), ("lessons", "about")):
                    db.execute(
                        f"DELETE FROM {table} WHERE group_key=? AND {column}=?",
                        (group, sender),
                    )

    def forget(self, group, item_id, actor):
        with self.tx() as db:
            events = [e for e in self.events(group) if e["item_id"] == item_id]
            if not events:
                raise ValueError("事项不存在")
            self._purge(
                db,
                group,
                {s for e in events for s in e["sources"]},
                actor,
                "forget_item",
            )
            db.execute(
                "DELETE FROM events WHERE group_key=? AND item_id=?", (group, item_id)
            )
            db.execute("DELETE FROM items WHERE group_key=? AND id=?", (group, item_id))

    def expire(self, group, days, now=None):
        cutoff = (
            (now or datetime.now(timezone.utc)) - timedelta(days=days)
        ).isoformat()
        ids = [
            r["uid"]
            for r in self.rows(
                "SELECT uid FROM messages WHERE group_key=? AND at<? AND erased=0",
                (group, cutoff),
            )
        ]
        if ids:
            with self.tx() as db:
                self._purge(db, group, ids, "system", "retention")
        # Generated-only conversations must expire even when no raw message expires.
        with self.tx() as db:
            db.execute("DELETE FROM drafts WHERE group_key=? AND at<?", (group, cutoff))
            db.execute(
                "DELETE FROM dialogue_runs WHERE group_key=? AND at<?", (group, cutoff)
            )
            db.execute(
                "DELETE FROM answers WHERE group_key=? AND at<?", (group, cutoff)
            )
            db.execute(
                "DELETE FROM episodes WHERE group_key=? AND end_at<?", (group, cutoff)
            )
            # Impressions and lessons that nothing confirmed for a whole period fade out.
            for table in ("profiles", "lessons"):
                db.execute(
                    f"DELETE FROM {table} WHERE group_key=? AND updated_at<?",
                    (group, cutoff),
                )
            for table in ("day_views", "digests"):
                db.execute(
                    f"DELETE FROM {table} WHERE group_key=? AND {'updated_at' if table == 'day_views' else 'at'}<?",
                    (group, cutoff),
                )
        return len(ids)

    def log_usage(self, group, role, model, tokens, seconds, error):
        with self.tx() as db:
            db.execute(
                "INSERT INTO usage(group_key,role,model,prompt_tokens,completion_tokens,cached_tokens,seconds,error,at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    group,
                    role,
                    model,
                    tokens.get("prompt_tokens"),
                    tokens.get("completion_tokens"),
                    tokens.get("cached_tokens"),
                    seconds,
                    error,
                    utcnow(),
                ),
            )

    def close(self):
        with self.lock:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.conn.close()
