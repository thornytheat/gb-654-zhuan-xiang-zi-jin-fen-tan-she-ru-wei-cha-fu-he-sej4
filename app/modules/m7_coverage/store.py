"""M7 资金覆盖对账持久化。

独立于 M1-M6 的 runs 存储：承诺与来源占用分表保存，
所有写操作在单个 BEGIN IMMEDIATE 事务内完成，保证并发争用同一来源不超额；
幂等键保证重试不重复占款。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cov_ledgers (
    ledger_id      TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL,
    state_json     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cov_sources (
    ledger_id      TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    cap            TEXT NOT NULL,
    locked         INTEGER NOT NULL,
    revoked        INTEGER NOT NULL,
    rev_seq        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ledger_id, source_id)
);
CREATE TABLE IF NOT EXISTS cov_commitments (
    ledger_id      TEXT NOT NULL,
    commitment_id  TEXT NOT NULL,
    project_id     TEXT NOT NULL,
    amount         TEXT NOT NULL,
    sources_json   TEXT NOT NULL,
    PRIMARY KEY (ledger_id, commitment_id)
);
CREATE TABLE IF NOT EXISTS cov_occupations (
    ledger_id      TEXT NOT NULL,
    commitment_id  TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    amount         TEXT NOT NULL,
    rev_seq        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ledger_id, commitment_id, source_id)
);
CREATE TABLE IF NOT EXISTS cov_gaps (
    ledger_id      TEXT NOT NULL,
    commitment_id  TEXT NOT NULL,
    gap            TEXT NOT NULL,
    reason_code    TEXT NOT NULL,
    rev_seq        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ledger_id, commitment_id)
);
CREATE TABLE IF NOT EXISTS cov_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ledger_id      TEXT NOT NULL,
    rev_seq        INTEGER NOT NULL,
    action         TEXT NOT NULL,
    commitment_id  TEXT,
    source_id      TEXT,
    amount         TEXT,
    before_value   TEXT,
    after_value    TEXT,
    reason_code    TEXT
);
CREATE TABLE IF NOT EXISTS cov_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    ledger_id       TEXT NOT NULL,
    kind            TEXT NOT NULL,
    response_json   TEXT NOT NULL
);
"""


class CoverageStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=5000")
            db.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        return db

    # ---- 幂等 ----
    def replay(self, key: str | None) -> dict | None:
        if not key:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT response_json FROM cov_idempotency WHERE idempotency_key=?",
                (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_replay(self, key: str | None, ledger_id: str, kind: str, response: dict) -> None:
        if not key:
            return
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO cov_idempotency VALUES (?,?,?,?)",
                (key, ledger_id, kind, json.dumps(response, ensure_ascii=False, sort_keys=True)))

    def ledger_exists(self, db: sqlite3.Connection, ledger_id: str) -> bool:
        return db.execute("SELECT 1 FROM cov_ledgers WHERE ledger_id=?",
                          (ledger_id,)).fetchone() is not None

    def load_ledger(self, ledger_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT state_json FROM cov_ledgers WHERE ledger_id=?",
                             (ledger_id,)).fetchone()
            if not row:
                return None
            state = json.loads(row[0])
            src_rows = db.execute(
                "SELECT * FROM cov_sources WHERE ledger_id=? ORDER BY source_id",
                (ledger_id,)).fetchall()
            com_rows = db.execute(
                "SELECT * FROM cov_commitments WHERE ledger_id=? ORDER BY commitment_id",
                (ledger_id,)).fetchall()
            occ_rows = db.execute(
                "SELECT * FROM cov_occupations WHERE ledger_id=? ORDER BY commitment_id, source_id",
                (ledger_id,)).fetchall()
            gap_rows = db.execute(
                "SELECT * FROM cov_gaps WHERE ledger_id=? ORDER BY commitment_id",
                (ledger_id,)).fetchall()
            event_rows = db.execute(
                "SELECT * FROM cov_events WHERE ledger_id=? ORDER BY id",
                (ledger_id,)).fetchall()
        return {
            "state": state,
            "sources": [dict(r) for r in src_rows],
            "commitments": [dict(r) for r in com_rows],
            "occupations": [dict(r) for r in occ_rows],
            "gaps": [dict(r) for r in gap_rows],
            "events": [dict(r) for r in event_rows],
        }

    def create_ledger(self, ledger_id: str, created_at: str, state: dict,
                      edges: dict, gaps: list[dict]) -> None:
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            if self.ledger_exists(db, ledger_id):
                raise _Conflict(ledger_id)
            db.execute("INSERT INTO cov_ledgers VALUES (?,?,?)",
                       (ledger_id, created_at,
                        json.dumps(state, ensure_ascii=False, sort_keys=True, default=str)))
            for sid in state["source_order"]:
                s = state["sources"][sid]
                db.execute("INSERT INTO cov_sources VALUES (?,?,?,?,?,0)",
                           (ledger_id, sid, str(s["cap"]), int(s["locked"]), int(s["revoked"])))
            for cid in state["commitment_order"]:
                c = state["commitments"][cid]
                db.execute("INSERT INTO cov_commitments VALUES (?,?,?,?,?)",
                           (ledger_id, cid, c["project_id"], str(c["amount"]),
                            json.dumps(c["sources"], ensure_ascii=False)))
            for (cid, sid), amount in sorted(edges.items()):
                db.execute("INSERT INTO cov_occupations VALUES (?,?,?,?,0)",
                           (ledger_id, cid, sid, str(amount)))
            for g in gaps:
                db.execute("INSERT INTO cov_gaps VALUES (?,?,?,?,0)",
                           (ledger_id, g["commitment_id"], g["gap"], g["reason_code"]))
            db.execute("COMMIT")
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def apply_revoke(self, ledger_id: str, revoked_sid: str, new_state: dict,
                     new_edges: dict, gaps: list[dict], events: list[dict]) -> int:
        """在单个写事务内落账撤销裁决，返回本次裁决序号；重试恢复原裁决。"""
        db = self.connect()
        outcome = "commit"
        rev_seq = 0
        try:
            db.execute("BEGIN IMMEDIATE")
            if not self.ledger_exists(db, ledger_id):
                outcome = "missing"
            else:
                current = db.execute(
                    "SELECT revoked, rev_seq FROM cov_sources WHERE ledger_id=? AND source_id=?",
                    (ledger_id, revoked_sid)).fetchone()
                if current is None:
                    outcome = "missing"
                elif current["revoked"]:
                    # 来源此前已撤销（含重试）：恢复原裁决，不重复占款
                    outcome = "already"
                else:
                    # 写锁内分配裁决序号，并发撤销同一来源串行化、不串号
                    rev_seq = int(db.execute(
                        "SELECT COALESCE(MAX(rev_seq),0) AS m FROM cov_sources WHERE ledger_id=?",
                        (ledger_id,)).fetchone()["m"]) + 1
                    db.execute(
                        "UPDATE cov_sources SET revoked=1, rev_seq=? WHERE ledger_id=? AND source_id=?",
                        (rev_seq, ledger_id, revoked_sid))
                    # 重写占用；锁定来源边金额保持不变
                    db.execute("DELETE FROM cov_occupations WHERE ledger_id=?", (ledger_id,))
                    for (cid, sid), amount in sorted(new_edges.items()):
                        db.execute("INSERT INTO cov_occupations VALUES (?,?,?,?,?)",
                                   (ledger_id, cid, sid, str(amount), rev_seq))
                    db.execute("DELETE FROM cov_gaps WHERE ledger_id=?", (ledger_id,))
                    for g in gaps:
                        db.execute("INSERT INTO cov_gaps VALUES (?,?,?,?,?)",
                                   (ledger_id, g["commitment_id"], g["gap"],
                                    g["reason_code"], rev_seq))
                    # 撤销前后事件只增不改
                    for e in events:
                        db.execute(
                            "INSERT INTO cov_events "
                            "(ledger_id,rev_seq,action,commitment_id,source_id,amount,"
                            "before_value,after_value,reason_code) VALUES (?,?,?,?,?,?,?,?,?)",
                            (ledger_id, rev_seq, e["action"], e["commitment_id"], e["source_id"],
                             e["amount"], e["before"], e["after"], e["reason_code"]))
                    db.execute(
                        "UPDATE cov_ledgers SET state_json=? WHERE ledger_id=?",
                        (json.dumps(new_state, ensure_ascii=False, sort_keys=True,
                                    default=str), ledger_id))
            if outcome == "commit":
                db.execute("COMMIT")
            else:
                db.rollback()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        if outcome == "missing":
            raise _Missing(ledger_id)
        if outcome == "already":
            raise _AlreadyApplied(ledger_id)
        return rev_seq


class _Conflict(Exception):
    def __init__(self, ledger_id: str):
        self.ledger_id = ledger_id


class _Missing(Exception):
    pass


class _AlreadyApplied(Exception):
    pass
