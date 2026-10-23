#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""本地主动联系调度器（不依赖 QQ / OneBot / Tulpa，不调用模型）。

职责只有「什么时候允许发起」：
- 读本地授权（memory/consent.json 的 local 记录，缺省回退 state/rules.json 的本地默认）
- 安静时段、每日/滚动上限、冷却、未回复锁、任务有效期
- 到期时生成一个 pending 提示（含原因与约束），交给本地 composer/人来写正文
- `--accept` 把写好的正文写入本地 inbox，并记录成功；`--skip` 取消

不负责：生成正文、发送到任何网络、桌面通知（可后续加）。

用法:
    python pipeline/outreach.py --status
    python pipeline/outreach.py --seed --reason follow_up --due 2026-10-24T09:00:00+08:00
    python pipeline/outreach.py --tick
    python pipeline/outreach.py --pending
    python pipeline/outreach.py --accept pt_xxx --text "..."
    python pipeline/outreach.py --skip pt_xxx
    python pipeline/outreach.py --user-said
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import notify as notifier

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "state" / "outreach.sqlite3"
CONFIG = ROOT / "state" / "outreach.json"
INBOX = ROOT / "runtime" / "outreach" / "inbox.jsonl"
PENDING = ROOT / "runtime" / "outreach" / "pending"
NOTIFICATIONS = ROOT / "runtime" / "outreach" / "notifications.jsonl"
INTERACTION = ROOT / "memory" / "interaction.jsonl"
CONSENT = ROOT / "memory" / "consent.json"
RULES = ROOT / "state" / "rules.json"
SCHEDULE = ROOT / "state" / "schedule.json"
COMMITMENTS = ROOT / "memory" / "commitments.jsonl"
EVENTS = ROOT / "memory" / "events.jsonl"

DEFAULT_CONFIG = {
    "enabled": False,
    "quiet_hours": ["22:00", "09:00"],
    "use_learned_schedule": True,
    "daily_cap": 1,
    "rolling_24h_cap": 1,
    "cooldown_hours": 6,
    "task_ttl_hours": 24,
    "stop_after_unanswered": True,
    "max_attempts": 2,
    "lease_minutes": 30,
    "channel": "local",
}

BANNED = ("你怎么不回", "为什么不回", "我一直在等", "我等你", "不理我", "是不是不想理")


class Reject(RuntimeError):
    pass


def now_local(value: str | None = None) -> datetime:
    if value:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise Reject("时间必须包含时区，例如 +08:00")
        return parsed.astimezone()
    return datetime.now().astimezone()


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def parse_hm(text: str) -> int:
    hour, minute = text.split(":")
    return int(hour) * 60 + int(minute)


def in_quiet(dt: datetime, config: dict) -> bool:
    quiet = config.get("quiet_hours") or []
    if len(quiet) != 2:
        return False
    current = dt.hour * 60 + dt.minute
    start, end = parse_hm(quiet[0]), parse_hm(quiet[1])
    if start > end:
        return current >= start or current < end
    return start <= current < end


def in_quiet_windows(dt: datetime, windows: list[list[int]]) -> bool:
    hour = dt.hour
    return any(start <= hour < end for start, end in windows if start != end)


def consent_scope(scope: str, consent_path: Path = CONSENT,
                  rules_path: Path = RULES) -> tuple[bool, str]:
    """读取本地 local 记录里的某个 scope；缺省回退 rules 的本地默认。"""
    if consent_path.exists():
        try:
            data = json.loads(consent_path.read_text("utf-8"))
        except ValueError:
            return False, "consent.json 无法解析，按关闭处理"
        if not isinstance(data, dict) or not isinstance(data.get("subjects", {}), dict):
            return False, "授权结构不合法"
        rec = (data.get("subjects") or {}).get("local") or {}
        if not isinstance(rec, dict) or not isinstance(rec.get("scopes", {}), dict):
            return False, "授权结构不合法"
        value = (rec.get("scopes") or {}).get(scope)
        if isinstance(value, bool):
            return value, "memory/consent.json 的 local 记录"
        if isinstance(value, dict) and isinstance(value.get("granted"), bool):
            return value["granted"], "memory/consent.json 的 local 记录"
        return False, f"consent.json 存在但没有 local.{scope}，按关闭处理"
    try:
        rules = json.loads(rules_path.read_text("utf-8"))
        default = (rules.get("consent") or {}).get("default") or {}
        return default.get(scope) is True, "state/rules.json 的本地默认"
    except (OSError, ValueError, TypeError, AttributeError):
        return False, "读不到授权配置，按关闭处理"


def proactive_allowed(consent_path: Path = CONSENT, rules_path: Path = RULES) -> tuple[bool, str]:
    return consent_scope("proactive", consent_path, rules_path)


def learned_schedule(path: Path = SCHEDULE, consent_path: Path = CONSENT,
                     rules_path: Path = RULES, max_age_days: int = 30) -> dict | None:
    """只有 schedule 授权开启、文件有效且不过期时才返回学习结果。"""
    allowed, _ = consent_scope("schedule", consent_path, rules_path)
    if not allowed or not path.exists():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
    except ValueError:
        return None
    if data.get("state") != "ok" or not data.get("quiet_windows"):
        return None
    generated = data.get("generated_at")
    if generated:
        try:
            age = datetime.now().astimezone() - datetime.fromisoformat(generated)
            if age.days > max_age_days:
                return None
        except (ValueError, TypeError):
            return None
    return data


class LocalOutreach:
    def __init__(self, *, db: Path = DB, config: Path = CONFIG, inbox: Path = INBOX,
                 pending: Path = PENDING, interaction: Path = INTERACTION,
                 consent: Path = CONSENT, rules: Path = RULES,
                 schedule: Path = SCHEDULE, commitments: Path = COMMITMENTS,
                 events: Path = EVENTS, notifications: Path = NOTIFICATIONS,
                 notify_enabled: bool = True):
        self.db = db
        self.config_path = config
        self.inbox = inbox
        self.pending = pending
        self.interaction = interaction
        self.consent = consent
        self.rules = rules
        self.schedule = schedule
        self.commitments = commitments
        self.events = events
        self.notifications = notifications
        self.notify_enabled = notify_enabled

    def _notify(self, title: str, body: str) -> None:
        if not self.notify_enabled:
            return
        try:
            notifier.notify(title, body, log=self.notifications)
        except Exception:
            pass

    # ---- 基础设施 ----
    def config(self) -> dict:
        cfg = dict(DEFAULT_CONFIG)
        if self.config_path.exists():
            cfg.update(json.loads(self.config_path.read_text("utf-8")))
        return cfg

    def connect(self) -> sqlite3.Connection:
        self.db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db, timeout=30)
        con.execute("""CREATE TABLE IF NOT EXISTS tasks(
            id TEXT PRIMARY KEY, reason TEXT, due_at TEXT, created_at TEXT, expires_at TEXT,
            status TEXT, attempts INTEGER DEFAULT 0, lease_until TEXT, delivered_at TEXT,
            message TEXT, payload TEXT, source_id TEXT)""")
        columns = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
        if "source_id" not in columns:
            con.execute("ALTER TABLE tasks ADD COLUMN source_id TEXT")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS task_source ON tasks(source_id) WHERE source_id IS NOT NULL")
        con.execute("CREATE TABLE IF NOT EXISTS deliveries("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, text TEXT, status TEXT,"
                    "created_at TEXT, delivered_at TEXT)")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_delivery_per_task ON deliveries(task_id)")
        con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
        return con

    @contextmanager
    def _conn(self):
        con = self.connect()
        try:
            con.commit()
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        finally:
            con.close()

    def meta_get(self, con, key: str, default=None):
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def meta_set(self, con, key: str, value: str):
        con.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value))

    # ---- 规则 ----
    def last_user_at(self) -> str | None:
        rows = [r for r in read_jsonl(self.interaction)
                if r.get("speaker") == "user" and r.get("subject") in (None, "local")]
        return max((r.get("ts") for r in rows if r.get("ts")), default=None)

    def _delivered(self, con, since: datetime | None = None) -> list[str]:
        rows = con.execute("SELECT delivered_at FROM tasks WHERE status='delivered' AND delivered_at IS NOT NULL").fetchall()
        out = []
        for (ts,) in rows:
            when = datetime.fromisoformat(ts)
            if since is None or when >= since:
                out.append(ts)
        return out

    def block_reason(self, con, now: datetime, cfg: dict, *, exclude_task_id: str = "") -> str | None:
        if not cfg.get("enabled", True):
            return "outreach.enabled=false"
        allowed, why = proactive_allowed(self.consent, self.rules)
        if not allowed:
            return f"未授权主动联系（{why}）"
        learned = learned_schedule(self.schedule, self.consent, self.rules) if cfg.get("use_learned_schedule", True) else None
        # 固定安静时段是硬限制；学习结果只能在固定限制之外再收紧，不能放宽。
        if in_quiet(now, cfg):
            return f"安静时段 {cfg.get('quiet_hours')}"
        if learned and in_quiet_windows(now, learned.get("quiet_windows") or []):
            return f"学习到的安静时段 {learned.get('quiet_windows')}"
        last_success = self.meta_get(con, "last_proactive_success_at")
        if last_success:
            elapsed = now - datetime.fromisoformat(last_success)
            if elapsed < timedelta(hours=float(cfg.get("cooldown_hours", 6))):
                return f"冷却中（上次 {last_success}）"
        # 已占用但尚未落盘的投递也占用额度，防止不同任务并发超限。
        reservations = con.execute(
            "SELECT created_at FROM deliveries WHERE status='pending' AND task_id!=?",
            (exclude_task_id,)).fetchall()
        reserved = [datetime.fromisoformat(r[0]) for r in reservations]
        if reserved:
            return "另一个投递正在处理；先恢复或完成该投递"
        today = now.date()
        delivered = [datetime.fromisoformat(x).astimezone(now.tzinfo) for x in self._delivered(con)]
        if sum(1 for d in delivered if d.date() == today) >= int(cfg.get("daily_cap", 1)):
            return "达到每日上限"
        if sum(1 for d in delivered if now - d < timedelta(hours=24)) >= int(cfg.get("rolling_24h_cap", 1)):
            return "达到 24h 滚动上限"
        if cfg.get("stop_after_unanswered", True) and self.meta_get(con, "outstanding_proactive") == "1":
            last_user = self.last_user_at() or self.meta_get(con, "last_user_activity_at")
            if not last_user or (last_success and last_user <= last_success):
                return "上一次主动未获回复，暂停"
        return None

    # ---- 操作 ----
    def add(self, reason: str, due: datetime, *, ttl_hours: float | None = None,
            payload: dict | None = None, source_id: str | None = None) -> dict:
        if due.tzinfo is None or due.utcoffset() is None:
            raise Reject("due 必须包含时区")
        due = due.astimezone()
        cfg = self.config()
        ttl = float(ttl_hours if ttl_hours is not None else cfg.get("task_ttl_hours", 24))
        now = datetime.now().astimezone()
        if source_id:
            with self._conn() as con:
                row = con.execute("SELECT id,status FROM tasks WHERE source_id=?", (source_id,)).fetchone()
                if row:
                    return {"task_id": row[0], "status": row[1], "source_id": source_id, "duplicate": True}
        key = hashlib.sha1(f"{reason}|{due.isoformat()}|{now.isoformat()}".encode()).hexdigest()[:6]
        task_id = f"pt_{now.strftime('%Y%m%d%H%M%S')}_{key}"
        with self._conn() as con:
            con.execute("INSERT INTO tasks(id,reason,due_at,created_at,expires_at,status,payload,source_id) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (task_id, reason, due.isoformat(), now.isoformat(),
                         (due + timedelta(hours=ttl)).isoformat(), "pending",
                         json.dumps(payload or {}, ensure_ascii=False), source_id))
        return {"task_id": task_id, "due_at": due.isoformat(), "status": "pending", "source_id": source_id}

    def record_commitment(self, text: str, due: datetime) -> dict:
        """记录一个承诺，并生成到期任务（source_id 去重）。"""
        now = datetime.now().astimezone()
        cid = "cm_" + hashlib.sha1(f"{text}|{due.isoformat()}|{now.isoformat()}".encode()).hexdigest()[:10]
        record = {"id": cid, "text": text, "due_at": due.isoformat(),
                  "created_at": now.isoformat(), "status": "open"}
        self.commitments.parent.mkdir(parents=True, exist_ok=True)
        with self.commitments.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        task = self.add("promise_due", due, payload={"commitment_id": cid, "text": text},
                        source_id=f"commitment:{cid}")
        return {"commitment": record, "task": task}

    def record_event(self, text: str, at: datetime) -> dict:
        """记录一个一次性事件提醒，并生成任务。"""
        now = datetime.now().astimezone()
        eid = "ev_" + hashlib.sha1(f"{text}|{at.isoformat()}|{now.isoformat()}".encode()).hexdigest()[:10]
        record = {"id": eid, "text": text, "at": at.isoformat(),
                  "created_at": now.isoformat(), "status": "open"}
        self.events.parent.mkdir(parents=True, exist_ok=True)
        with self.events.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        task = self.add("event_reminder", at, payload={"event_id": eid, "text": text},
                        source_id=f"event:{eid}")
        return {"event": record, "task": task}

    def scan(self, now: datetime | None = None, horizon_hours: float = 48) -> dict:
        """从承诺/事件文件补建任务；已建过的靠 source_id 去重。"""
        now = now or now_local()
        horizon = now + timedelta(hours=horizon_hours)
        created, skipped = [], []
        for row in read_jsonl(self.commitments):
            if row.get("status") != "open":
                continue
            due = datetime.fromisoformat(row["due_at"])
            if due > horizon:
                continue
            result = self.add("promise_due", due, payload={"commitment_id": row.get("id"),
                                                           "text": row.get("text", "")},
                              source_id=f"commitment:{row.get('id')}")
            (skipped if result.get("duplicate") else created).append(result.get("task_id"))
        for row in read_jsonl(self.events):
            if row.get("status") != "open":
                continue
            at = datetime.fromisoformat(row["at"])
            if at > horizon:
                continue
            result = self.add("event_reminder", at, payload={"event_id": row.get("id"),
                                                             "text": row.get("text", "")},
                              source_id=f"event:{row.get('id')}")
            (skipped if result.get("duplicate") else created).append(result.get("task_id"))
        return {"created": created, "skipped": skipped}

    def tick(self, now: datetime | None = None) -> dict:
        now = now or now_local()
        cfg = self.config()
        self.pending.mkdir(parents=True, exist_ok=True)
        allowed, why = proactive_allowed(self.consent, self.rules)
        with self._conn() as con:
            con.execute("UPDATE tasks SET status='cancelled' WHERE status='pending' AND expires_at<?",
                        (now.isoformat(),))
            expired_planned = con.execute(
                "SELECT id FROM tasks WHERE status='planned' AND expires_at<=?", (now.isoformat(),)).fetchall()
            if expired_planned:
                con.execute("UPDATE tasks SET status='expired' WHERE status='planned' AND expires_at<=?",
                            (now.isoformat(),))
                for (task_id,) in expired_planned:
                    (self.pending / f"{task_id}.json").unlink(missing_ok=True)
            max_attempts = int(cfg.get("max_attempts", 2))
            con.execute("UPDATE tasks SET status='failed' WHERE status='planned' "
                        "AND lease_until<? AND attempts>=?", (now.isoformat(), max_attempts))
            reclaimed = con.execute(
                "UPDATE tasks SET status='pending', attempts=attempts+1 "
                "WHERE status='planned' AND lease_until<? AND attempts<?",
                (now.isoformat(), max_attempts)).rowcount
            if reclaimed:
                print(f"reclaimed {reclaimed} expired lease(s)", file=sys.stderr)
            if not cfg.get("enabled", True) or not allowed:
                cancelled = con.execute(
                    "UPDATE tasks SET status='cancelled' WHERE status IN ('pending','planned')").rowcount
                for path in self.pending.glob("*.json"):
                    path.unlink()
                reason = "outreach.enabled=false" if not cfg.get("enabled", True) else f"未授权主动联系（{why}）"
                return {"state": "blocked", "reason": reason, "emitted": [], "cancelled": cancelled}
            block = self.block_reason(con, now, cfg)
            if block:
                return {"state": "blocked", "reason": block, "emitted": []}
            rows = con.execute(
                "SELECT id,reason,due_at,expires_at,payload FROM tasks "
                "WHERE status='pending' AND due_at<=? ORDER BY due_at LIMIT 5",
                (now.isoformat(),)).fetchall()
            emitted = []
            for task_id, reason, due_at, expires_at, payload in rows:
                doc = {
                    "task_id": task_id,
                    "reason": reason,
                    "due_at": due_at,
                    "expires_at": expires_at,
                    "channel": cfg.get("channel", "local"),
                    "payload": json.loads(payload or "{}"),
                    "constraints": {
                        "length": "1–3 句",
                        "no_demand": True,
                        "no_guilt": True,
                        "no_fake_user_quote": True,
                        "quiet_hours": cfg.get("quiet_hours"),
                        "banned_phrases": list(BANNED),
                    },
                    "prompt": (
                        "为本地会话写一条主动消息。只写正文，1–3 句；不索取回应、不卖惨、"
                        "不质问对方为什么不回、不假装对方说过话。"
                    ),
                }
                path = self.pending / f"{task_id}.json"
                path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8", newline="\n")
                con.execute("UPDATE tasks SET status='planned', lease_until=? WHERE id=?",
                            ((now + timedelta(minutes=int(cfg.get("lease_minutes", 30)))).isoformat(), task_id))
                emitted.append(str(path))
            if emitted:
                self._notify("红莉栖有一条主动消息待确认", f"{len(emitted)} 条 pending，等待 compose/accept。")
            return {"state": "ok" if emitted else "idle", "emitted": emitted}

    def _load_task(self, con, task_id: str):
        row = con.execute("SELECT status,due_at,expires_at,reason FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise Reject(f"任务不存在: {task_id}")
        return row

    def accept(self, task_id: str, text: str, now: datetime | None = None,
               *, override: bool = False) -> dict:
        now = now or now_local()
        text = " ".join(text.split()).strip()
        if not text or len(text) > 500:
            raise Reject("正文必须为 1–500 字")
        for phrase in BANNED:
            if phrase in text:
                raise Reject(f"正文含不允许的施压/质问表达: {phrase}")
        with self._conn() as con:
            status, due_at, expires_at, reason = self._load_task(con, task_id)
            # force 仅允许人工跳过 tick，不可重新打开终态或绕过授权/时间/频率。
            allowed_states = {"planned", "pending"} if override else {"planned"}
            if status not in allowed_states:
                raise Reject(f"任务状态为 {status}；中断投递请使用 recover")
            if datetime.fromisoformat(due_at) > now:
                raise Reject("任务未到期")
            if datetime.fromisoformat(expires_at) <= now:
                raise Reject("任务已过期")
            block = self.block_reason(con, now, self.config())
            if block:
                raise Reject(f"发送前检查不通过：{block}")
            con.execute("UPDATE tasks SET status='delivering' WHERE id=?", (task_id,))
            con.execute("INSERT INTO deliveries(task_id,text,status,created_at) VALUES(?,?,?,?)",
                        (task_id, text, "pending", now.isoformat()))
        # 先持久化唯一投递意图，再写 inbox；崩溃恢复可对照 task_id 对账。
        return self._deliver_reserved(task_id, now, override=override)

    def _deliver_reserved(self, task_id: str, now: datetime, *, recovered: bool = False,
                          override: bool = False) -> dict:
        rejected = None
        with self._conn() as con:
            row = con.execute("SELECT id,text,status FROM deliveries WHERE task_id=?",
                              (task_id,)).fetchone()
            if not row:
                raise Reject("没有可恢复的投递意图")
            delivery_id, text, delivery_status = row
            status, due_at, expires_at, reason = self._load_task(con, task_id)
            existing = next((r for r in read_jsonl(self.inbox) if r.get("task_id") == task_id), None)
            if delivery_status == "done":
                return {"task_id": task_id, "status": "delivered", "duplicate": True}
            if existing is None:
                if status != "delivering" or delivery_status != "pending":
                    rejected = "任务已取消或不可恢复"
                elif datetime.fromisoformat(expires_at) <= now:
                    rejected = "任务已过期"
                elif datetime.fromisoformat(due_at) > now:
                    rejected = "任务未到期"
                else:
                    # 恢复并非新授权，落盘前再次读取当前权限与全部发送限制。
                    rejected = self.block_reason(con, now, self.config(), exclude_task_id=task_id)
                if rejected:
                    con.execute("UPDATE deliveries SET status='cancelled' WHERE id=?", (delivery_id,))
                    con.execute("UPDATE tasks SET status=? WHERE id=?",
                                ("expired" if datetime.fromisoformat(expires_at) <= now else "cancelled", task_id))
                else:
                    existing = {"task_id": task_id, "ts": now.isoformat(), "channel": "local",
                                "status": "delivered", "text": text, "recovered": recovered,
                                "override": bool(override)}
                    self.inbox.parent.mkdir(parents=True, exist_ok=True)
                    with self.inbox.open("a", encoding="utf-8", newline="\n") as fh:
                        fh.write(json.dumps(existing, ensure_ascii=False, sort_keys=True) + "\n")
                        fh.flush()
                        import os
                        os.fsync(fh.fileno())
            if not rejected:
                # inbox 已有记录时只对账；沿用实际写入时间，不重复投递。
                delivered_at = existing["ts"]
                con.execute("UPDATE deliveries SET status='done', delivered_at=? WHERE id=?",
                            (delivered_at, delivery_id))
                con.execute("UPDATE tasks SET status='delivered', delivered_at=?, message=? WHERE id=?",
                            (delivered_at, text, task_id))
                last = self.meta_get(con, "last_proactive_success_at")
                if not last or datetime.fromisoformat(delivered_at) >= datetime.fromisoformat(last):
                    self.meta_set(con, "last_proactive_success_at", delivered_at)
                    self.meta_set(con, "outstanding_proactive", "1")
        (self.pending / f"{task_id}.json").unlink(missing_ok=True)
        if rejected:
            raise Reject(f"恢复/投递前检查不通过：{rejected}")
        self._notify("主动消息已写入本地 inbox", text[:80])
        return {"task_id": task_id, "status": "delivered", "inbox": str(self.inbox),
                "override": bool(override), "delivery_id": delivery_id}

    def skip(self, task_id: str, reason: str = "manual_skip") -> dict:
        with self._conn() as con:
            status, *_ = self._load_task(con, task_id)
            if status == "delivered":
                raise Reject("已投递的任务不可重新取消")
            con.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task_id,))
            con.execute("UPDATE deliveries SET status='cancelled' WHERE task_id=? AND status='pending'", (task_id,))
        (self.pending / f"{task_id}.json").unlink(missing_ok=True)
        return {"task_id": task_id, "status": "cancelled", "reason": reason}

    def recover(self, now: datetime | None = None) -> dict:
        """对账已写入记录；未写入记录必须重新通过当前授权和发送限制。"""
        now = now or now_local()
        with self._conn() as con:
            rows = con.execute("SELECT task_id FROM deliveries WHERE status='pending' ORDER BY id").fetchall()
        recovered, failed = [], []
        for (task_id,) in rows:
            try:
                self._deliver_reserved(task_id, now, recovered=True)
                recovered.append(task_id)
            except Reject:
                failed.append(task_id)
        return {"recovered": recovered, "failed": failed}

    def forget_local(self) -> dict:
        """清理本模块的任务、待处理提示、inbox、通知、承诺与事件。"""
        with self._conn() as con:
            con.execute("DELETE FROM tasks")
            con.execute("DELETE FROM deliveries")
            con.execute("DELETE FROM meta")
        removed = 0
        for path in (self.inbox, self.notifications):
            if path and Path(path).exists():
                Path(path).unlink()
                removed += 1
        if self.pending.exists():
            for path in self.pending.glob("*.json"):
                path.unlink()
                removed += 1
            try:
                self.pending.rmdir()
            except OSError:
                pass
        for path in (self.commitments, self.events):
            if Path(path).exists():
                Path(path).unlink()
                removed += 1
        return {"tasks": "cleared", "files_removed": removed}

    def user_said(self, now: datetime | None = None) -> dict:
        now = now or now_local()
        with self._conn() as con:
            self.meta_set(con, "outstanding_proactive", "0")
            self.meta_set(con, "last_user_activity_at", now.isoformat())
        return {"outstanding_proactive": False, "at": now.isoformat()}

    def status(self, now: datetime | None = None) -> dict:
        now = now or now_local()
        cfg = self.config()
        learned = learned_schedule(self.schedule, self.consent, self.rules) if cfg.get("use_learned_schedule", True) else None
        with self._conn() as con:
            rows = con.execute("SELECT id,reason,due_at,status FROM tasks ORDER BY due_at").fetchall()
            block = self.block_reason(con, now, cfg)
            pending_files = sorted(str(p) for p in self.pending.glob("*.json")) if self.pending.exists() else []
            return {
                "enabled": cfg.get("enabled"),
                "quiet_hours": cfg.get("quiet_hours"),
                "learned_schedule": ({"generated_at": learned.get("generated_at"),
                                      "quiet_windows": learned.get("quiet_windows"),
                                      "confidence": learned.get("confidence")} if learned else None),
                "blocked_by": block,
                "outstanding_proactive": self.meta_get(con, "outstanding_proactive") == "1",
                "last_proactive_success_at": self.meta_get(con, "last_proactive_success_at"),
                "last_user_activity_at": self.meta_get(con, "last_user_activity_at"),
                "tasks": [{"id": r[0], "reason": r[1], "due_at": r[2], "status": r[3]} for r in rows],
                "pending_prompts": pending_files,
            }


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--tick", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--reason", default="follow_up")
    ap.add_argument("--due")
    ap.add_argument("--accept")
    ap.add_argument("--force", action="store_true", help="人工跳过 tick；仍检查授权、到期、安静时段和频率")
    ap.add_argument("--text")
    ap.add_argument("--skip")
    ap.add_argument("--user-said", action="store_true")
    ap.add_argument("--pending", action="store_true")
    ap.add_argument("--commit", metavar="TEXT", help="记录一个承诺并生成到期任务")
    ap.add_argument("--event", metavar="TEXT", help="记录一个一次性事件提醒并生成任务")
    ap.add_argument("--at", help="--event 的提醒时间（ISO）")
    ap.add_argument("--scan", action="store_true", help="从 memory/commitments.jsonl 与 events.jsonl 补建任务")
    ap.add_argument("--recover", action="store_true", help="恢复中断的投递（pending delivery / delivering）")
    ap.add_argument("--forget-local", action="store_true", help="清空本地提醒任务、inbox、通知、承诺与事件")
    ap.add_argument("--horizon-hours", type=float, default=48)
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--now")
    args = ap.parse_args(argv)

    engine = LocalOutreach(notify_enabled=not args.no_notify)
    now = now_local(args.now) if args.now else None

    if args.status:
        print(json.dumps(engine.status(now), ensure_ascii=False, indent=2))
        return 0
    if args.seed:
        due = datetime.fromisoformat(args.due) if args.due else (now or now_local()) + timedelta(minutes=1)
        print(json.dumps(engine.add(args.reason, due), ensure_ascii=False))
        return 0
    if args.commit:
        if not args.due:
            print("--commit 需要 --due", file=sys.stderr)
            return 2
        print(json.dumps(engine.record_commitment(args.commit, datetime.fromisoformat(args.due)),
                         ensure_ascii=False))
        return 0
    if args.event:
        if not args.at:
            print("--event 需要 --at", file=sys.stderr)
            return 2
        print(json.dumps(engine.record_event(args.event, datetime.fromisoformat(args.at)),
                         ensure_ascii=False))
        return 0
    if args.scan:
        print(json.dumps(engine.scan(now, args.horizon_hours), ensure_ascii=False))
        return 0
    if args.recover:
        print(json.dumps(engine.recover(now), ensure_ascii=False))
        return 0
    if args.forget_local:
        print(json.dumps(engine.forget_local(), ensure_ascii=False))
        return 0
    if args.tick:
        result = engine.tick(now)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["state"] != "blocked" else 3
    if args.pending:
        files = sorted(engine.pending.glob("*.json")) if engine.pending.exists() else []
        for path in files:
            print(path.read_text("utf-8"))
        print(f"pending={len(files)}")
        return 0
    if args.accept:
        try:
            print(json.dumps(engine.accept(args.accept, args.text or "", now, override=args.force),
                             ensure_ascii=False))
            return 0
        except Reject as exc:
            print(f"REJECT {exc}", file=sys.stderr)
            return 2
    if args.skip:
        print(json.dumps(engine.skip(args.skip), ensure_ascii=False))
        return 0
    if args.user_said:
        print(json.dumps(engine.user_said(now), ensure_ascii=False))
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
