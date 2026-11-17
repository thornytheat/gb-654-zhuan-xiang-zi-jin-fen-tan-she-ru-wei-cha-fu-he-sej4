"""M7 资金覆盖对账服务：编排核心裁决与持久化，组装回读视图。"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal

from app.errors import DomainError
from app.manifest import canonical_digest
from . import core
from .store import CoverageStore, _AlreadyApplied, _Conflict, _Missing

SAMPLE = {
    "ledger_id": "ledger-demo",
    "sources": [
        {"source_id": "S1", "cap": "600.00", "locked": False},
        {"source_id": "S2", "cap": "500.00", "locked": False},
        {"source_id": "S3", "cap": "300.00", "locked": True},
    ],
    "commitments": [
        {"commitment_id": "C1", "project_id": "P1", "amount": "500.00", "sources": ["S1", "S2"]},
        {"commitment_id": "C2", "project_id": "P2", "amount": "400.00", "sources": ["S1", "S3"]},
        {"commitment_id": "C3", "project_id": "P3", "amount": "300.00", "sources": ["S3"]},
    ],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_store(request) -> CoverageStore:
    store = getattr(request.app.state, "coverage_store", None)
    if store is None:
        store = CoverageStore(request.app.state.settings.db_path + ".coverage")
        request.app.state.coverage_store = store
    return store


def _view(ledger_id: str, data: dict) -> dict:
    """把落库记录组装为回读视图：每笔承诺的来源、缺口与撤销前后记录。"""
    state = data["state"]
    occupations = data["occupations"]
    # 按承诺汇总来源占用
    by_commit: dict[str, list] = {}
    source_used: dict[str, Decimal] = {}
    for row in occupations:
        by_commit.setdefault(row["commitment_id"], []).append(
            {"source_id": row["source_id"], "amount": row["amount"],
             "locked": bool(state["sources"][row["source_id"]]["locked"]),
             "revoked": bool(state["sources"][row["source_id"]]["revoked"])})
        source_used[row["source_id"]] = source_used.get(row["source_id"], Decimal(0)) + Decimal(row["amount"])
    commitments_view = []
    for cid in state["commitment_order"]:
        c = state["commitments"][cid]
        committed = Decimal(str(c["amount"]))
        held = sum((Decimal(x["amount"]) for x in by_commit.get(cid, [])), Decimal(0))
        gap_row = next((g for g in data["gaps"] if g["commitment_id"] == cid), None)
        commitments_view.append({
            "commitment_id": cid,
            "project_id": c["project_id"],
            "committed": str(committed.quantize(core.QUANTUM)),
            "covered": str(held.quantize(core.QUANTUM)),
            "gap": str((committed - held).quantize(core.QUANTUM)),
            "eligible_sources": list(c["sources"]),
            "occupations": sorted(by_commit.get(cid, []), key=lambda x: x["source_id"]),
            "gap_reason": gap_row["reason_code"] if gap_row else None,
        })
    sources_view = []
    for sid in state["source_order"]:
        s = state["sources"][sid]
        cap = Decimal(str(s["cap"]))
        used = source_used.get(sid, Decimal(0))
        sources_view.append({
            "source_id": sid,
            "cap": str(cap),
            "locked": bool(s["locked"]),
            "revoked": bool(s["revoked"]),
            "occupied": str(used),
            "available": str(cap - used),
        })
    return {
        "ledger_id": ledger_id,
        "rule_version": "2026.11-m7",
        "rev_seq": max((r["rev_seq"] for r in data["sources"]), default=0),
        "sources": sources_view,
        "commitments": commitments_view,
        "gaps": [{"commitment_id": g["commitment_id"], "gap": g["gap"],
                  "reason_code": g["reason_code"], "rev_seq": g["rev_seq"]}
                 for g in data["gaps"]],
        "events": [
            {"rev_seq": e["rev_seq"], "action": e["action"],
             "commitment_id": e["commitment_id"], "source_id": e["source_id"],
             "amount": e["amount"], "before": e["before_value"],
             "after": e["after_value"], "reason_code": e["reason_code"]}
            for e in data["events"]
        ],
        "totals": {
            "committed": str(sum((Decimal(c["committed"]) for c in commitments_view), Decimal(0))),
            "covered": str(sum((Decimal(c["covered"]) for c in commitments_view), Decimal(0))),
            "gap": str(sum((Decimal(c["gap"]) for c in commitments_view), Decimal(0))),
        },
    }


def open_ledger(store: CoverageStore, payload: dict | None = None) -> dict:
    payload = payload or SAMPLE
    ledger_id = str(payload.get("ledger_id") or ("ledger-" + canonical_digest(payload)[:12]))
    idem = payload.get("idempotency_key")
    replay = store.replay(idem)
    if replay is not None:
        return {**replay, "idempotent": True, "reason": replay.get("reason", "OK")}
    built = core.build_ledger(payload.get("sources", SAMPLE["sources"]),
                              payload.get("commitments", SAMPLE["commitments"]))
    state, edges, gaps = built["state"], built["edges"], built["gaps"]
    edge_view = [{"commitment_id": c, "source_id": s, "amount": str(v)}
                 for (c, s), v in sorted(edges.items())]
    try:
        store.create_ledger(ledger_id, _now(), state, edges, gaps)
    except _Conflict:
        existing = store.load_ledger(ledger_id)
        return {**_view(ledger_id, existing), "status": "READY", "idempotent": True,
                "reason": "LEDGER_EXISTS", "edges": edge_view}
    view = _view(ledger_id, store.load_ledger(ledger_id))
    view.update({"status": "READY", "idempotent": False,
                 "reason": "OK" if not gaps else "GAP", "edges": edge_view})
    # 以完整回读视图作为幂等重放载荷，保证重试返回与首次一致
    store.save_replay(idem, ledger_id, "ledger", view)
    return view


def revoke_source(store: CoverageStore, payload: dict) -> dict:
    ledger_id = str(payload["ledger_id"])
    sid = str(payload["source_id"])
    idem = payload.get("idempotency_key")
    replay = store.replay(idem)
    if replay is not None:
        return {**replay, "idempotent": True, "reason": "IDEMPOTENT_REPLAY"}
    data = store.load_ledger(ledger_id)
    if data is None:
        raise DomainError("LEDGER_NOT_FOUND", f"台账不存在: {ledger_id}")
    if not any(s["source_id"] == sid for s in data["sources"]):
        raise DomainError(core.NOT_FOUND, f"来源不存在: {sid}")
    already = next(s["revoked"] for s in data["sources"] if s["source_id"] == sid)
    # 深拷贝状态，保证纯函数裁决不污染回读前的落库快照
    state = copy.deepcopy(data["state"])
    # JSON 回读后 Decimal 已变为字符串，重新水合为核心引擎所需类型
    for s in state["sources"].values():
        s["cap"] = Decimal(str(s["cap"]))
        s["locked"] = bool(s["locked"])
        s["revoked"] = bool(s["revoked"])
    for c in state["commitments"].values():
        c["amount"] = Decimal(str(c["amount"]))
        c["sources"] = list(c["sources"])
    result = core.reconcile_revoke(state, data["occupations"], sid)
    # 先落库（撤销前后事件只增不改），再组装回读与幂等响应；rev_seq 在写事务内分配
    try:
        store.apply_revoke(ledger_id, sid, state, result["edges"], result["gaps"],
                           result["events"])
    except _AlreadyApplied:
        replay = store.replay(idem)
        if replay is not None:
            return {**replay, "idempotent": True, "reason": "IDEMPOTENT_REPLAY"}
        return {**_view(ledger_id, store.load_ledger(ledger_id)),
                "status": "READY", "idempotent": True, "reason": "REVOKE_ALREADY_APPLIED"}
    except _Missing:
        raise DomainError("LEDGER_NOT_FOUND", f"台账或来源不存在: {ledger_id}/{sid}")
    view = _view(ledger_id, store.load_ledger(ledger_id))
    view.update({"status": "READY", "idempotent": False,
                 "revoked_source_id": sid, "already_revoked": bool(already),
                 "reason": "OK" if not result["gaps"] else "GAP"})
    store.save_replay(idem, ledger_id, "revoke", view)
    return view


def read_ledger(store: CoverageStore, ledger_id: str) -> dict:
    data = store.load_ledger(ledger_id)
    if data is None:
        raise DomainError("LEDGER_NOT_FOUND", f"台账不存在: {ledger_id}")
    return _view(ledger_id, data)


def read_ledger_demo(store: CoverageStore) -> dict:
    """页面初始视图：无台账时用样例建账，有则回读最近台账。"""
    ledger_id = SAMPLE["ledger_id"]
    data = store.load_ledger(ledger_id)
    if data is None:
        return open_ledger(store, SAMPLE)
    return _view(ledger_id, data)
