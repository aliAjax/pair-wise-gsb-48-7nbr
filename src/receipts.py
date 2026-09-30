"""外部存管交收回执：领域校验与对账分类。

外部存管方回传的回执以批次送达，常出现重复批次、乱序明细，或晚于本地
结算指令修改到达。这里只放无副作用的规则，幂等落库、状态推进由
repository 负责，权限与编排在 service 层。
"""
from typing import Any, Dict, List, Optional

from .domain import ValidationError, text, choice, number

# 明细状态
ITEM_MATCHED = "matched"            # 引用存在且引用的是当前版本
ITEM_REF_MISSING = "ref_missing"    # 本地查无此引用
ITEM_VERSION_CHANGED = "version_changed"  # 回执引用的版本已过期/超前
ITEM_CONFIRMED = "confirmed"        # 主管复核后确认
ITEM_REJECTED = "rejected"          # 主管复核后驳回

# 触发待复核、会拦住审批/交收的状态
PENDING_STATES = {ITEM_REF_MISSING, ITEM_VERSION_CHANGED}
# 主管可对一笔明细做的复核决定
DECISIONS = {ITEM_CONFIRMED, ITEM_REJECTED}

RESULT_CHOICES = ["settled", "failed"]

CUSTODY_SUBMIT_ROLE = "custody_clerk"
CUSTODY_REVIEW_ROLE = "custody_supervisor"


def validate_batch(payload: Dict[str, Any]) -> Dict[str, Any]:
    """校验外部回传的回执批次请求体。"""
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是对象")
    batch_no = text(payload, "batch_no")
    items = payload.get("items", [])
    if not isinstance(items, list) or not items:
        raise ValidationError("items至少需要一笔回执")
    if len(items) > 1000:
        raise ValidationError("单批次回执不能超过1000笔")
    normalized: List[Dict[str, Any]] = []
    seen_keys = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValidationError("items第%s项必须是对象" % (index + 1))
        reference = text(item, "reference")
        # 交收结果与数量/金额：外部存管对交收完成情况的确认
        result = choice(item, "result", RESULT_CHOICES)
        delivered = number(item, "delivered_quantity", 0)
        cash = number(item, "cash_paid", 0)
        ref_version = item.get("ref_version")
        if not isinstance(ref_version, int) or isinstance(ref_version, bool) or ref_version < 1:
            raise ValidationError("items第%s项ref_version必须是正整数" % (index + 1))
        receipt_no = str(item.get("receipt_no", "")).strip()
        # 同批次内重复明细：优先按存管流水号，其次按业务键去重
        dedup_key = receipt_no or "%s#%s" % (reference, ref_version)
        if dedup_key in seen_keys:
            raise ValidationError("批次内回执重复：%s" % dedup_key)
        seen_keys.add(dedup_key)
        normalized.append({
            "receipt_no": receipt_no,
            "reference": reference,
            "ref_version": ref_version,
            "result": result,
            "delivered_quantity": delivered,
            "cash_paid": cash,
        })
    return {"batch_no": batch_no, "items": normalized}


def classify_item(reference_version: Optional[int], current_version: Optional[int], same_org: bool) -> str:
    """根据本地指令是否存在、版本是否一致对一笔回执分类。

    reference_version/current_version 为 None 表示本地查无引用（乱序早到）；
    跨机构引用同样按缺失处理，避免把别的机构指令当成可对账目标。
    """
    if reference_version is None or current_version is None or not same_org:
        return ITEM_REF_MISSING
    if int(reference_version) != int(current_version):
        return ITEM_VERSION_CHANGED
    return ITEM_MATCHED


def pending_from_states(states: List[str]) -> bool:
    return any(state in PENDING_STATES for state in states)


def batch_status(item_states: List[str]) -> str:
    """由明细状态汇总批次状态。"""
    if pending_from_states(item_states):
        return "pending_review"
    if all(state in {ITEM_CONFIRMED, ITEM_REJECTED} for state in item_states):
        return "reviewed"
    return "matched"
