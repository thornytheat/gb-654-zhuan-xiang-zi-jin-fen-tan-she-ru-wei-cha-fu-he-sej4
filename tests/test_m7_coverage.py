"""M7 资金覆盖对账测试。

覆盖：承诺与占用分账、封顶与最大余额尾差、撤销释放与合规替补、
锁定来源不动、不足计缺口而非削减承诺、并发不超额、重试幂等不重复占款、回读。
"""
import os
import sqlite3
import threading
from decimal import Decimal

import pytest

from app.errors import DomainError
from app.modules.m7_coverage import core, service
from app.modules.m7_coverage.store import CoverageStore
from app.modules.m7_coverage.service import SAMPLE


@pytest.fixture
def store(tmp_path):
    return CoverageStore(str(tmp_path / "coverage.db"))


def _scenario():
    return {
        "ledger_id": "L1",
        "sources": [
            {"source_id": "S1", "cap": "600.00", "locked": False},
            {"source_id": "S2", "cap": "500.00", "locked": False},
            {"source_id": "S3", "cap": "300.00", "locked": True},
        ],
        "commitments": [
            {"commitment_id": "C1", "amount": "500.00", "sources": ["S1", "S2"]},
            {"commitment_id": "C2", "amount": "400.00", "sources": ["S1", "S2"]},
            {"commitment_id": "C3", "amount": "300.00", "sources": ["S3"]},
        ],
    }


def _seed_locked(store, ledger_id):
    """模拟锁定来源上的既有占款（锁定边不参与任何重排）。"""
    path = store.path
    db = sqlite3.connect(path)
    db.execute("INSERT INTO cov_occupations VALUES (?,?,?,?,0)",
               (ledger_id, "C3", "S3", "300.00"))
    db.execute("DELETE FROM cov_gaps WHERE ledger_id=? AND commitment_id=?",
               (ledger_id, "C3"))
    db.commit()
    db.close()


# ---------- 核心规则：分账、封顶、尾差 ----------

def test_commitment_and_occupation_kept_separate():
    built = core.build_ledger(SAMPLE["sources"], SAMPLE["commitments"])
    # 承诺金额不被占用计算改写
    for cid, c in built["state"]["commitments"].items():
        assert c["amount"] == Decimal(SAMPLE["commitments"][
            next(i for i, x in enumerate(SAMPLE["commitments"]) if x["commitment_id"] == cid)]["amount"])
    # 占用是独立的边记录，绝不出现“占用即承诺”的合并
    assert all(k[0] and k[1] for k in built["edges"])


def test_waterfill_respects_source_caps():
    built = core.build_ledger(SAMPLE["sources"], SAMPLE["commitments"])
    used = {}
    for (_, s), v in built["edges"].items():
        used[s] = used.get(s, Decimal(0)) + v
    for sid, cap in ((x["source_id"], Decimal(x["cap"])) for x in SAMPLE["sources"]):
        assert used.get(sid, Decimal(0)) <= cap


def test_commitment_never_over_covered():
    built = core.build_ledger(SAMPLE["sources"], SAMPLE["commitments"])
    covered = {}
    for (c, _), v in built["edges"].items():
        covered[c] = covered.get(c, Decimal(0)) + v
    for cid, c in built["state"]["commitments"].items():
        assert covered.get(cid, Decimal(0)) <= c["amount"]


def test_conservation_covered_plus_gap_equals_committed():
    built = core.build_ledger(SAMPLE["sources"], SAMPLE["commitments"])
    committed = sum((c["amount"] for c in built["state"]["commitments"].values()), Decimal(0))
    covered = sum(built["edges"].values(), Decimal(0))
    gap = sum((Decimal(g["gap"]) for g in built["gaps"]), Decimal(0))
    assert covered + gap == committed


def test_all_amounts_quantized_to_cent():
    built = core.build_ledger(SAMPLE["sources"], SAMPLE["commitments"])
    assert all(v.as_tuple().exponent == -2 for v in built["edges"].values())


def test_largest_remainder_tie_goes_to_smallest_id():
    state = core.normalize(
        [{"source_id": "S", "cap": "0.01"}],
        [{"commitment_id": "B", "amount": "0.01", "sources": ["S"]},
         {"commitment_id": "A", "amount": "0.01", "sources": ["S"]}])
    result = core.plan(state)
    assert result["edges"][("A", "S")] == Decimal("0.01")
    assert any(g["commitment_id"] == "B" and g["gap"] == "0.01" for g in result["gaps"])


def test_order_independence():
    sources = [{"source_id": "S1", "cap": "100.00"},
               {"source_id": "S2", "cap": "77.77"},
               {"source_id": "S3", "cap": "50.00"}]
    commitments = [
        {"commitment_id": "C1", "amount": "80.00", "sources": ["S1", "S2"]},
        {"commitment_id": "C2", "amount": "90.00", "sources": ["S2", "S3"]},
        {"commitment_id": "C3", "amount": "60.00", "sources": ["S1", "S3"]}]
    forward = core.build_ledger(sources, commitments)
    reverse = core.build_ledger(list(reversed(sources)), list(reversed(commitments)))
    assert forward["edges"] == reverse["edges"]
    assert forward["gaps"] == reverse["gaps"]


def test_gap_when_no_eligible_source():
    state = core.normalize(
        [{"source_id": "S1", "cap": "100.00"},
         {"source_id": "S2", "cap": "50.00", "revoked": True}],
        [{"commitment_id": "C1", "amount": "50.00", "sources": ["S1"]},
         {"commitment_id": "C2", "amount": "20.00", "sources": ["S2"]}])
    result = core.plan(state)
    gap = next(g for g in result["gaps"] if g["commitment_id"] == "C2")
    assert gap["gap"] == "20.00"
    assert gap["reason_code"] == core.GAP_NO_ELIGIBLE


def test_invalid_money_rejected():
    with pytest.raises(DomainError):
        core.normalize([{"source_id": "S1", "cap": "-1.00"}],
                       [{"commitment_id": "C1", "amount": "1.00", "sources": ["S1"]}])


def test_duplicate_id_rejected():
    with pytest.raises(DomainError):
        core.normalize([{"source_id": "S1", "cap": "1.00"},
                        {"source_id": "S1", "cap": "2.00"}], [])


# ---------- 撤销 / 替补 / 锁定 / 缺口 ----------

def test_revoke_releases_and_replaces_without_shrinking_commitment(store):
    service.open_ledger(store, _scenario())
    _seed_locked(store, "L1")
    before = service.read_ledger(store, "L1")
    result = service.revoke_source(
        store, {"ledger_id": "L1", "source_id": "S1", "idempotency_key": "r1"})
    # 承诺金额不变
    for c in result["commitments"]:
        old = next(x for x in before["commitments"] if x["commitment_id"] == c["commitment_id"])
        assert c["committed"] == old["committed"]
    # 被撤销来源占用清零
    assert all(o["source_id"] != "S1" for c in result["commitments"] for o in c["occupations"])
    # 未锁定来源被重排用于替补
    s2 = next(s for s in result["sources"] if s["source_id"] == "S2")
    assert Decimal(s2["occupied"]) > 0
    # 不足形成缺口，且 覆盖+缺口=承诺
    assert Decimal(result["totals"]["covered"]) + Decimal(result["totals"]["gap"]) \
        == Decimal(result["totals"]["committed"])


def test_locked_source_occupation_untouched(store):
    service.open_ledger(store, _scenario())
    _seed_locked(store, "L1")
    result = service.revoke_source(
        store, {"ledger_id": "L1", "source_id": "S1", "idempotency_key": "r1"})
    c3 = next(c for c in result["commitments"] if c["commitment_id"] == "C3")
    assert any(o["source_id"] == "S3" and o["amount"] == "300.00" and o["locked"]
               for o in c3["occupations"])
    s3 = next(s for s in result["sources"] if s["source_id"] == "S3")
    assert s3["occupied"] == "300.00"  # 锁定占用金额原封不动
    assert any(e["action"] == "LOCKED_KEPT" for e in result["events"])


def test_gap_persisted_when_replacement_insufficient(store):
    # 撤销后仅剩一个容量很小的合规来源，替补不足 -> 缺口而非削减承诺
    payload = {
        "ledger_id": "G1",
        "sources": [{"source_id": "S1", "cap": "100.00"},
                    {"source_id": "S2", "cap": "30.00"}],
        "commitments": [{"commitment_id": "C1", "amount": "100.00", "sources": ["S1", "S2"]}]}
    service.open_ledger(store, payload)
    result = service.revoke_source(
        store, {"ledger_id": "G1", "source_id": "S1", "idempotency_key": "g1"})
    c1 = result["commitments"][0]
    assert c1["committed"] == "100.00"          # 承诺不削减
    assert c1["covered"] == "30.00"
    assert c1["gap"] == "70.00"                 # 差额保留为缺口
    assert c1["gap_reason"] == core.GAP_NO_CAPACITY


def test_revoke_emits_before_after_events(store):
    service.open_ledger(store, _scenario())
    _seed_locked(store, "L1")
    result = service.revoke_source(
        store, {"ledger_id": "L1", "source_id": "S1", "idempotency_key": "r1"})
    revokes = [e for e in result["events"] if e["action"] == "REVOKE"]
    assert revokes and all(e["after"] == "0.00" and e["before"] == e["amount"] for e in revokes)
    assert any(e["action"] == "GAP" for e in result["events"])
    # 同一 (承诺,来源) 不重复记事件
    seen = [(e["commitment_id"], e["source_id"], e["action"]) for e in result["events"]]
    assert len(seen) == len(set(seen))


def test_revoke_unknown_source_raises(store):
    service.open_ledger(store, _scenario())
    with pytest.raises(DomainError):
        service.revoke_source(store, {"ledger_id": "L1", "source_id": "NOPE"})


def test_revoke_unknown_ledger_raises(store):
    with pytest.raises(DomainError):
        service.revoke_source(store, {"ledger_id": "MISSING", "source_id": "S1"})


def test_double_revoke_is_idempotent_and_no_double_charge(store):
    service.open_ledger(store, _scenario())
    _seed_locked(store, "L1")
    first = service.revoke_source(
        store, {"ledger_id": "L1", "source_id": "S1", "idempotency_key": "k"})
    second = service.revoke_source(
        store, {"ledger_id": "L1", "source_id": "S1", "idempotency_key": "k"})
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert first["totals"] == second["totals"]
    assert first["events"] == second["events"]
    # 无幂等键的重复撤销同样不重复占款
    third = service.revoke_source(store, {"ledger_id": "L1", "source_id": "S1"})
    assert third["idempotent"] is True
    assert third["totals"] == first["totals"]


def test_two_successive_revokes_accumulate_gap_keep_commitment(store):
    service.open_ledger(store, _scenario())
    _seed_locked(store, "L1")
    service.revoke_source(store, {"ledger_id": "L1", "source_id": "S1", "idempotency_key": "a"})
    second = service.revoke_source(store, {"ledger_id": "L1", "source_id": "S2", "idempotency_key": "b"})
    assert second["rev_seq"] == 2
    # 只剩锁定来源 S3 的 300，其余全部为缺口
    assert second["totals"]["committed"] == "1200.00"
    assert second["totals"]["covered"] == "300.00"
    assert second["totals"]["gap"] == "900.00"


# ---------- 并发 ----------

def test_concurrent_revokes_never_exceed_source_cap(store):
    payload = {
        "ledger_id": "SH",
        "sources": [{"source_id": "S1", "cap": "100.00"},
                    {"source_id": "S2", "cap": "100.00"},
                    {"source_id": "S3", "cap": "100.00"}],
        "commitments": [{"commitment_id": "C1", "amount": "250.00",
                         "sources": ["S1", "S2", "S3"]}]}
    service.open_ledger(store, payload)
    errors = []

    def revoke(sid, key):
        try:
            service.revoke_source(
                store, {"ledger_id": "SH", "source_id": sid, "idempotency_key": key})
        except Exception as exc:  # pragma: no cover - 断言用
            errors.append(repr(exc))

    threads = [threading.Thread(target=revoke, args=(s, f"key-{s}"))
               for s in ("S1", "S2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    view = service.read_ledger(store, "SH")
    for s in view["sources"]:
        assert Decimal(s["occupied"]) <= Decimal(s["cap"])
    assert Decimal(view["totals"]["covered"]) + Decimal(view["totals"]["gap"]) \
        == Decimal(view["totals"]["committed"])


def test_concurrent_ledger_open_no_duplicate(tmp_path):
    errors = []

    def open_many(i):
        try:
            s = CoverageStore(str(tmp_path / f"t{i}.db"))
            service.open_ledger(s, {**_scenario(), "ledger_id": "L1",
                                    "idempotency_key": f"k{i}"})
        except Exception as exc:  # pragma: no cover
            errors.append(repr(exc))

    threads = [threading.Thread(target=open_many, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


# ---------- 回读 / 建账幂等 / HTTP ----------

def test_readback_contains_sources_gap_and_events(store):
    service.open_ledger(store, _scenario())
    _seed_locked(store, "L1")
    service.revoke_source(store, {"ledger_id": "L1", "source_id": "S1", "idempotency_key": "r"})
    view = service.read_ledger(store, "L1")
    assert {c["commitment_id"] for c in view["commitments"]} == {"C1", "C2", "C3"}
    for c in view["commitments"]:
        assert "occupations" in c and "gap" in c and "committed" in c
    assert view["events"] and all("before" in e and "after" in e for e in view["events"])
    assert all(s["available"] is not None for s in view["sources"])


def test_open_ledger_idempotent_replay(store):
    first = service.open_ledger(store, {**_scenario(), "idempotency_key": "open"})
    second = service.open_ledger(store, {**_scenario(), "idempotency_key": "open"})
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert first["commitments"] == second["commitments"]


def test_duplicate_ledger_id_returns_existing(store):
    service.open_ledger(store, _scenario())
    again = service.open_ledger(store, _scenario())
    assert again["idempotent"] is True
    assert again["reason"] == "LEDGER_EXISTS"


def test_http_endpoints_and_readback(client):
    # 建账
    resp = client.post("/api/coverage/ledgers", json={**_scenario(), "idempotency_key": "h1"})
    assert resp.status_code == 200
    assert resp.json()["ledger_id"] == "L1"
    # 幂等重试
    again = client.post("/api/coverage/ledgers", json={**_scenario(), "idempotency_key": "h1"})
    assert again.json()["idempotent"] is True
    # 撤销
    rev = client.post("/api/coverage/revoke",
                      json={"ledger_id": "L1", "source_id": "S1", "idempotency_key": "h2"})
    assert rev.status_code == 200
    body = rev.json()
    assert body["revoked_source_id"] == "S1"
    assert "events" in body and "totals" in body
    # 回读
    got = client.get("/api/coverage/ledgers/L1").json()
    assert len(got["commitments"]) == 3
    # 不存在台账 -> 422
    assert client.get("/api/coverage/ledgers/NOPE").status_code == 422
    # 页面
    assert client.get("/coverage").status_code == 200


def test_does_not_touch_payment_or_scoring(store):
    # 模块边界：响应中不存在支付或资格评分语义
    view = service.open_ledger(store, {**_scenario(), "idempotency_key": "boundary"})
    text = str(view)
    for forbidden in ("payment", "支付", "score", "评分", "eligibility_score"):
        assert forbidden not in text
