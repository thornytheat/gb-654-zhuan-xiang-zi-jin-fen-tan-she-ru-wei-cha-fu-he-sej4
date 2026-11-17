REASONS = {
    "BAD_COLUMNS": "字段数量不符", "EMPTY_VALUE": "必要值为空",
    "DUPLICATE_CONFLICT": "同一业务键取值冲突", "TRUNCATED": "尾记录不完整",
    "INVALID_VALUE": "值不符合定义", "STALE_VERSION": "提交版本已过期",
    "LEDGER_NOT_FOUND": "资金覆盖台账不存在",
    "SOURCE_NOT_FOUND": "资金来源不存在",
    "SOURCE_REVOKED": "来源已撤销，原占用已释放",
    "SOURCE_LOCKED": "来源已锁定，占用不参与重排",
    "SOURCE_ALREADY_REVOKED": "来源此前已撤销",
    "GAP_NO_ELIGIBLE_SOURCE": "无合规可用来源，承诺保留为缺口",
    "GAP_SOURCE_CAPACITY": "合规来源余额不足，差额保留为缺口",
}

def reason(code: str) -> str:
    return REASONS.get(code, "未登记原因")
