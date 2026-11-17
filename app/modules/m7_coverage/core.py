"""M7 资金覆盖对账核心规则。

承诺(commitment)与来源占用(occupation)分开记账：
- 承诺是项目已确认的获资金额，撤销来源时承诺金额不变；
- 占用是某笔承诺在某个来源上的预留，可释放、可替补、可重排；
- 替补不足时保留缺口(gap)，绝不削减承诺。

封顶沿用 M4 的迭代封顶思路，到分尾差沿用 M5 的最大余额法。
所有裁决按编号排序，输入顺序变化不改变结果。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN

from app.errors import DomainError

QUANTUM = Decimal("0.01")
ZERO = Decimal("0.00")

# 原因码（与 app/reasons.py 的稳定码风格一致）
GAP_NO_ELIGIBLE = "GAP_NO_ELIGIBLE_SOURCE"   # 承诺没有任何可用指定来源
GAP_NO_CAPACITY = "GAP_SOURCE_CAPACITY"      # 合规来源余额不足
SRC_REVOKED = "SOURCE_REVOKED"
SRC_LOCKED = "SOURCE_LOCKED"
NOT_FOUND = "SOURCE_NOT_FOUND"


def money(value, field: str) -> Decimal:
    try:
        amount = Decimal(str(value).replace(",", ""))
    except Exception:
        raise DomainError("INVALID_VALUE", f"{field} 金额非法: {value!r}")
    if not amount.is_finite() or amount < 0:
        raise DomainError("INVALID_VALUE", f"{field} 金额不能为负: {value!r}")
    return amount.quantize(QUANTUM)


def _sorted_ids(rows, key):
    ids = [r[key] for r in rows]
    if len(set(ids)) != len(ids):
        raise DomainError("DUPLICATE_CONFLICT", f"{key} 存在重复编号")
    return sorted(ids)


def normalize(sources: list[dict], commitments: list[dict]) -> dict:
    """校验并归一输入，返回内部状态。"""
    source_ids = _sorted_ids(sources, "source_id")
    commit_ids = _sorted_ids(commitments, "commitment_id")
    src = {}
    for row in sources:
        sid = str(row["source_id"])
        src[sid] = {
            "source_id": sid,
            "cap": money(row.get("cap", 0), f"source {sid}.cap"),
            "locked": bool(row.get("locked", False)),
            "revoked": bool(row.get("revoked", False)),
        }
    com = {}
    for row in commitments:
        cid = str(row["commitment_id"])
        allowed = []
        for sid in row.get("sources", []):
            sid = str(sid)
            if sid not in src:
                raise DomainError("INVALID_VALUE", f"承诺 {cid} 指定了不存在的来源 {sid}")
            if sid not in allowed:
                allowed.append(sid)
        com[cid] = {
            "commitment_id": cid,
            "project_id": str(row.get("project_id", cid)),
            "amount": money(row.get("amount", 0), f"commitment {cid}.amount"),
            "sources": allowed,
        }
    return {"sources": src, "commitments": com,
            "source_order": source_ids, "commitment_order": commit_ids}


def waterfill(needs: dict, room: dict, eligible: dict):
    """封顶水位法：在 承诺-来源 二部图上同时满足各方需求。

    needs: {cid: 尚需金额(>0)}；room: {sid: 来源剩余额度(>0)}；
    eligible: {cid: [sid,...]} 按声明偏好排序。
    返回 (精确占用边 {(cid,sid): Decimal}, 各承诺剩余需求)。
    与输入顺序无关：每轮按编号排序裁决，封顶来源取满自身额度，不争抢他人额度。
    """
    need = {c: v for c, v in needs.items() if v > 0}
    cap_left = {s: v for s, v in room.items()}
    edges: dict[tuple, Decimal] = {}
    while True:
        active = [c for c in sorted(need)
                  if need[c] > 0 and any(cap_left.get(s, ZERO) > 0 for s in eligible[c])]
        if not active:
            break
        pools = {c: [s for s in eligible[c] if cap_left.get(s, ZERO) > 0] for c in active}
        proposal = {s: ZERO for s in cap_left}
        share: dict[tuple, Decimal] = {}
        for c in active:
            p = need[c] / Decimal(len(pools[c]))
            for s in pools[c]:
                share[(c, s)] = p
                proposal[s] += p
        capping = sorted(s for s in proposal
                         if cap_left.get(s, ZERO) > 0 and proposal[s] >= cap_left[s])
        if not capping:
            # 没有来源触顶：各承诺在当前可用来源间均分，一次填满
            for (c, s), p in share.items():
                edges[(c, s)] = edges.get((c, s), ZERO) + p
            for c in active:
                need[c] = ZERO
            break
        for s in capping:
            total_prop = proposal[s]
            r = cap_left[s]
            for c in active:
                if (c, s) not in share:
                    continue
                part = r * share[(c, s)] / total_prop if total_prop > 0 else ZERO
                edges[(c, s)] = edges.get((c, s), ZERO) + part
                need[c] -= part
            cap_left[s] = ZERO
    return edges, need


def _to_cents(amount: Decimal) -> int:
    return int((amount / QUANTUM).to_integral_value(rounding=ROUND_DOWN))


def _largest_remainder_cents(total_cents: int, raw: dict) -> dict:
    """把 total_cents 个 1 分按最大余额法分到各键，余数相同按键名升序。

    raw 为各键的精确权重（Decimal 元），按其折算的分占比决定尾差归属。
    """
    keys = list(raw)
    out = {k: 0 for k in keys}
    if total_cents <= 0 or not keys:
        return out
    raw_sum = sum(raw.values())
    if raw_sum <= 0:
        out[min(keys)] = total_cents  # 无权重时全部给键序最小者，保持确定性
        return out
    raw_cents = {k: raw[k] / QUANTUM for k in keys}
    floors = {k: int(raw_cents[k]) for k in keys}  # 向下取整到分
    leftover = total_cents - sum(floors.values())
    order = sorted(keys, key=lambda k: (-(raw_cents[k] - int(raw_cents[k])), k))
    for k in order[:max(leftover, 0)]:
        floors[k] += 1
    return floors


def round_edges(exact_edges: dict, needs: dict, caps: dict) -> dict:
    """把精确占用边落到分。

    先逐条向下取整，再按来源用最大余额法分配该来源的尾差分；
    最后修复因进位导致的承诺超额（同一来源内把 1 分让给未补足承诺，
    无人可让则释放为来源空闲额度），保证：
    承诺占用合计 <= 承诺金额；来源占用合计 <= 来源封顶。
    """
    edges = {k: v for k, v in exact_edges.items() if v > 0}
    by_source: dict[str, list] = {}
    for (c, s) in edges:
        by_source.setdefault(s, []).append(c)
    rounded: dict[tuple, int] = {}
    for s, cs in by_source.items():
        exact = {c: edges[(c, s)] for c in cs}
        target_cents = min(_to_cents(sum(exact.values())), _to_cents(caps.get(s, ZERO)))
        cents = _largest_remainder_cents(target_cents, exact)
        for c in cs:
            if cents[c]:
                rounded[(c, s)] = cents[c]
    # 修复承诺超额
    covered: dict[str, int] = {}
    for (c, s), v in rounded.items():
        covered[c] = covered.get(c, 0) + v
    need_cents = {c: _to_cents(v) for c, v in needs.items()}
    for c in sorted(covered):
        while covered.get(c, 0) > need_cents.get(c, 0):
            for s in sorted(by_source):
                if rounded.get((c, s), 0) <= 0:
                    continue
                takers = sorted(t for t in by_source[s]
                                if covered.get(t, 0) < need_cents.get(t, 0))
                if takers:
                    t = takers[0]
                    rounded[(c, s)] -= 1
                    rounded[(t, s)] = rounded.get((t, s), 0) + 1
                    covered[c] -= 1
                    covered[t] = covered.get(t, 0) + 1
                else:
                    # 无人可让：释放该分，来源占用低于封顶（允许）
                    rounded[(c, s)] -= 1
                    covered[c] -= 1
                break
            else:
                break
    return {k: Decimal(v) * QUANTUM for k, v in rounded.items() if v > 0}


def plan(state: dict, fixed_edges: dict | None = None) -> dict:
    """在给定状态上求覆盖方案。

    fixed_edges: 已锁定占用 {(cid,sid): Decimal)}，锁定来源不参与重排；
    其余承诺按全额需求在未锁定、未撤销来源上重排。
    返回 quantized 占用边与每笔承诺缺口。
    """
    src, com = state["sources"], state["commitments"]
    fixed_edges = fixed_edges or {}
    # 锁定占用与撤销来源不进入可分配池
    locked_sources = {s for s in src if src[s]["locked"]}
    dead_sources = {s for s in src if src[s]["revoked"]}
    fixed_by_commit: dict[str, Decimal] = {}
    fixed_by_source: dict[str, Decimal] = {}
    for (c, s), v in fixed_edges.items():
        fixed_by_commit[c] = fixed_by_commit.get(c, ZERO) + v
        fixed_by_source[s] = fixed_by_source.get(s, ZERO) + v

    needs = {c: com[c]["amount"] - fixed_by_commit.get(c, ZERO)
             for c in state["commitment_order"]}

    room = {}
    for s in state["source_order"]:
        if s in locked_sources or s in dead_sources:
            continue
        room[s] = src[s]["cap"] - fixed_by_source.get(s, ZERO)
        if room[s] < 0:
            raise DomainError("INVALID_VALUE", f"来源 {s} 锁定占用已超过封顶")
    eligible = {c: [s for s in com[c]["sources"]
                    if s not in locked_sources and s not in dead_sources]
                for c in needs}
    exact, remain = waterfill(needs, room, eligible)
    edges = round_edges(exact, needs, room)
    # 合并锁定边
    for k, v in fixed_edges.items():
        edges[k] = edges.get(k, ZERO) + v
    covered = {}
    for (c, s), v in edges.items():
        covered[c] = covered.get(c, ZERO) + v
    gaps = []
    for c in state["commitment_order"]:
        committed = com[c]["amount"]
        held = covered.get(c, ZERO)
        gap = committed - held
        if gap > 0:
            reason = GAP_NO_ELIGIBLE if not com[c]["sources"] else GAP_NO_CAPACITY
            # 有指定来源但全部失效（撤销/锁定且无替补）也归为无合规可用来源
            live = [s for s in com[c]["sources"]
                    if s not in dead_sources and s not in locked_sources]
            if not live:
                reason = GAP_NO_ELIGIBLE
            gaps.append({"commitment_id": c, "project_id": com[c]["project_id"],
                         "committed": str(committed), "covered": str(held),
                         "gap": str(gap), "reason_code": reason})
    return {"edges": edges, "gaps": gaps, "covered": covered}


def build_ledger(sources: list[dict], commitments: list[dict]) -> dict:
    """首次建账：承诺与来源占用分开记录。"""
    state = normalize(sources, commitments)
    result = plan(state)
    return {"state": state, "edges": result["edges"], "gaps": result["gaps"]}


def _edge_map(occupations: list[dict]) -> dict:
    out = {}
    for row in occupations:
        key = (str(row["commitment_id"]), str(row["source_id"]))
        out[key] = out.get(key, ZERO) + money(row["amount"], "occupation.amount")
    return out


def reconcile_revoke(state: dict, occupations: list[dict], revoke_source_id: str) -> dict:
    """纯函数裁决：撤销一个来源，释放原占用并尝试合规替补。

    已锁定来源的占用保持不动；未锁定来源的占用允许整体重排；
    替补不足形成缺口，承诺金额不变。返回新占用边、缺口与前后对比事件。
    """
    sid = str(revoke_source_id)
    if sid not in state["sources"]:
        raise DomainError(NOT_FOUND, f"来源不存在: {sid}")
    old_edges = _edge_map(occupations)
    events: list[dict] = []
    already = state["sources"][sid]["revoked"]
    state["sources"][sid]["revoked"] = True

    # 1) 记录被撤销来源上释放的承诺与金额（撤销前事实）
    released_need: dict[str, Decimal] = {}
    for (c, s), v in sorted(old_edges.items()):
        if s == sid:
            released_need[c] = released_need.get(c, ZERO) + v
    # 2) 锁定边固定，其余未锁定占用全部纳入重排（含未受撤销影响的承诺，腾挪互补）
    locked_sources = {s for s in state["sources"] if state["sources"][s]["locked"]}
    fixed = {k: v for k, v in old_edges.items() if k[1] in locked_sources}
    for (c, s), v in sorted(fixed.items()):
        events.append({"action": "LOCKED_KEPT", "commitment_id": c, "source_id": s,
                       "amount": str(v), "before": str(v), "after": str(v),
                       "reason_code": SRC_LOCKED})
    result = plan(state, fixed_edges=fixed)
    new_edges = result["edges"]

    # 3) 生成全部占用边的撤销前后对比事件（每个 (承诺,来源) 一条，不重复）
    keys = sorted(set(old_edges) | set(new_edges))
    for k in keys:
        c, s = k
        before = old_edges.get(k, ZERO)
        after = new_edges.get(k, ZERO)
        if s == sid:
            if before > 0:
                events.append({"action": "REVOKE", "commitment_id": c, "source_id": s,
                               "amount": str(before), "before": str(before), "after": "0.00",
                               "reason_code": SRC_REVOKED})
            continue
        if s in locked_sources:
            continue  # 锁定边已在 LOCKED_KEPT 记录，金额不变
        if before == after:
            continue
        if before == ZERO and after > ZERO:
            action = "REPLACE" if c in released_need else "RESERVE"
            reason = "REPLACEMENT_ELIGIBLE"
        elif after == ZERO and before > ZERO:
            action = "RELEASE"
            reason = "REPLAN_UNLOCKED"
        else:
            action = "ADJUST"
            reason = "REPLAN_UNLOCKED"
        events.append({"action": action, "commitment_id": c, "source_id": s,
                       "amount": str(abs(after - before)),
                       "before": str(before), "after": str(after), "reason_code": reason})
    # 4) 缺口事件（承诺不削减，不足保留为缺口）
    for gap in result["gaps"]:
        events.append({"action": "GAP", "commitment_id": gap["commitment_id"],
                       "source_id": None, "amount": gap["gap"],
                       "before": "0.00", "after": gap["gap"],
                       "reason_code": gap["reason_code"]})
    return {"revoked_source_id": sid, "already_revoked": already,
            "edges": new_edges, "gaps": result["gaps"], "events": events}
