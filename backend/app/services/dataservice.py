"""数据服务业务规则：状态流转、字段校验、接口契约快照与缓存签名都收在这里。

链路梳理（服务发布 → 缓存签名 → 大盘取数）：

1. 上线服务：把接口路径、查询参数（入参）、返回字段、服务版本冻结为接口契约快照，
   缓存签名取契约内容哈希，随契约版本变化；同一接口路径同时只允许一个生效契约
   （并发上线同一路径只能成功一个版本），重复上线幂等复用已有契约；
   重复发布相同契约得到同一缓存签名，不会形成多份缓存结果。
2. 查询数据服务结果：仅「已上线」服务可查（服务下线后不再返回新结果）；
   读取已发布数据集（realtimemetric 模块已上线指标）与当前生效契约，
   按契约冻结的返回字段投影生成数据服务响应数据快照；
   幂等键 =（缓存签名, 规范化请求参数），重复提交返回同一份快照，不生成第二份。
3. 契约升级：服务下线后重新上线（或同路径新服务接管）生成新契约版本与新缓存签名；
   查询只认当前生效契约，任一响应快照只归属一个契约版本，
   旧签名快照保留用于追溯、不再被新查询命中。
   （权衡：选择「单一生效契约 + 旧缓存只读追溯」，放弃多版本共存按签名路由，
   保证任意时刻一个路径的响应只对应一个契约版本。）
4. 监控大盘（下游消费方）：发布大盘与刷新大盘只读取已上线服务的当前契约，
   通过 collect_published_snapshots 同步消费响应快照，不直接改契约与快照。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "dataservice"
CONTRACT_MODULE = "dataservice_contract"
SNAPSHOT_MODULE = "dataservice_snapshot"
DATASET_MODULE = "realtimemetric"
DATASET_PUBLISHED_STATUS = "已上线"

REQUIRED_FIELDS = ["服务编号", "服务名称", "接口路径"]
ENTRY_FIELDS = [
    "服务编号", "服务名称", "接口路径", "查询参数",
    "返回字段", "调用频次限制", "缓存策略", "服务版本",
]
STATUS_ORDER = ["草稿", "服务评审中", "已上线", "已下线"]
ACTION_RULES = {"提交服务评审": "服务评审中", "上线服务": "已上线", "下线服务": "已下线"}
NEGATIVE_ACTIONS = ["下线服务"]

# 上线时冻结进契约快照的字段：接口路径、入参字段、返回字段、版本
CONTRACT_FIELDS = ["接口路径", "查询参数", "返回字段", "服务版本"]

# 上线检查与契约写入必须在同一把锁里完成，保证并发上线同一路径只成功一个版本
_PUBLISH_LOCK = threading.RLock()
_FIELD_SPLIT = re.compile(r"[,，、/；;\s]+")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _cache_signature(path: str, params: str, fields: str, version: str) -> str:
    """缓存签名 = 契约内容哈希；契约内容变化（升级）才会得到新签名。

    相同契约内容必然得到相同签名，因此重复发布相同契约会复用同一份缓存结果。
    """
    raw = json.dumps(
        {"接口路径": path, "查询参数": params, "返回字段": fields, "服务版本": version},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _normalize_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """规范化查询参数，保证同一语义的重复提交得到同一个幂等键。"""
    if not params:
        return {}
    text = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
    return json.loads(text)


def _split_fields(raw: Any) -> list[str]:
    """把契约里冻结的返回字段原文拆成字段列表，作为响应数据的冻结边界。"""
    return [part for part in _FIELD_SPLIT.split(str(raw or "").strip()) if part]


class DataserviceService:
    # ---- 列表与明细 -------------------------------------------------

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("服务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in ENTRY_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    # ---- 接口契约快照 ----------------------------------------------

    def _active_contract(self, service_id: int) -> dict[str, Any] | None:
        for row in store.rows(CONTRACT_MODULE):
            if row.get("服务id") == service_id and row.get("status") == "生效中":
                return row
        return None

    def _active_contract_by_path(self, path: str) -> dict[str, Any] | None:
        for row in store.rows(CONTRACT_MODULE):
            if row.get("接口路径") == path and row.get("status") == "生效中":
                return row
        return None

    def list_contracts(
        self,
        *,
        path: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(CONTRACT_MODULE)
        if path:
            rows = [row for row in rows if path in str(row.get("接口路径", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def _publish(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """上线：冻结接口契约快照并计算缓存签名。

        - 同一服务重复上线：幂等返回已有生效契约，不生成第二份；
        - 同一接口路径：同时只允许一个生效契约，并发上线只成功一个版本；
        - 契约版本按接口路径递增，保证路径维度的版本可追溯。
        """
        with _PUBLISH_LOCK:
            service_id = int(entry.get("id", 0))
            active = self._active_contract(service_id)
            if active is not None:
                return active, (
                    f"服务接口已上线，契约 v{active['契约版本']} 此前已冻结，"
                    f"缓存签名 {active['缓存签名']}（重复发布复用现有契约，未生成新快照）"
                )
            path = str(entry.get("接口路径") or "").strip()
            holder = self._active_contract_by_path(path)
            if holder is not None and int(holder.get("服务id", 0)) != service_id:
                return None, (
                    f"接口路径「{path}」已被服务 {holder.get('服务编号')} 的契约 "
                    f"v{holder.get('契约版本')} 占用，同一路径同时只能上线一个版本"
                )
            frozen = {field: str(entry.get(field) or "") for field in CONTRACT_FIELDS}
            version_no = sum(
                1 for row in store.rows(CONTRACT_MODULE) if row.get("接口路径") == path
            ) + 1
            signature = _cache_signature(
                frozen["接口路径"], frozen["查询参数"], frozen["返回字段"], frozen["服务版本"]
            )
            rows = store.rows(CONTRACT_MODULE)
            contract = {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "服务id": service_id,
                "服务编号": entry.get("服务编号"),
                **frozen,
                "契约版本": version_no,
                "缓存签名": signature,
                "status": "生效中",
                "冻结时间": _now(),
                "失效时间": None,
            }
            rows.append(contract)
            return contract, (
                f"服务接口已上线，契约 v{version_no} 已冻结"
                f"（接口路径/入参字段/返回字段/版本），缓存签名 {signature}"
            )

    def _unpublish(self, entry: dict[str, Any]) -> str:
        """下线：生效契约置为已失效，之后查询不再返回新结果（幂等）。"""
        with _PUBLISH_LOCK:
            active = self._active_contract(int(entry.get("id", 0)))
            if active is None:
                return "服务接口已下线（无生效契约，重复下线不产生新变化）"
            active["status"] = "已失效"
            active["失效时间"] = _now()
            return (
                f"服务接口已下线，契约 v{active['契约版本']}（缓存签名 {active['缓存签名']}）"
                f"已失效，不再返回新结果"
            )

    # ---- 状态流转 ---------------------------------------------------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"服务接口 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据服务可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "上线服务":
            contract, message = self._publish(entry)
            if contract is None:
                return None, message
        elif action == "下线服务":
            message = self._unpublish(entry)
        else:
            message = f"服务接口已{action}"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, message

    # ---- 查询数据服务结果与响应数据快照 ------------------------------

    def query(
        self, entry_id: int, params: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """查询数据服务结果，返回（响应快照, 说明, 是否命中既有缓存）。

        幂等键 =（缓存签名, 规范化请求参数）：重复提交返回同一份快照，不生成第二份。
        契约升级后缓存签名变化，旧签名快照不再命中，响应只归属当前生效契约版本。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"服务接口 {entry_id} 不存在或已归档", False
        if entry.get("status") != "已上线":
            return None, (
                f"服务接口当前状态为「{entry.get('status')}」，"
                "只有已上线服务才能查询，已下线服务不再返回新结果"
            ), False
        contract = self._active_contract(entry_id)
        if contract is None:
            return None, "服务接口缺少生效契约，请先上线服务冻结接口契约", False
        norm_params = _normalize_params(params)
        signature = str(contract["缓存签名"])
        for snapshot in store.rows(SNAPSHOT_MODULE):
            if snapshot.get("缓存签名") == signature and snapshot.get("请求参数") == norm_params:
                return snapshot, (
                    f"命中缓存签名 {signature} 的既有响应快照（契约 v{contract['契约版本']}），"
                    "重复提交未生成第二份快照"
                ), True
        dataset = [
            row for row in store.rows(DATASET_MODULE)
            if row.get("status") == DATASET_PUBLISHED_STATUS
        ]
        fields = _split_fields(contract.get("返回字段"))
        data = {
            field: [row.get(field) for row in dataset if row.get(field) is not None]
            for field in fields
        }
        rows = store.rows(SNAPSHOT_MODULE)
        snapshot = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "契约id": contract["id"],
            "服务id": entry_id,
            "服务编号": entry.get("服务编号"),
            "接口路径": contract["接口路径"],
            "契约版本": contract["契约版本"],
            "服务版本": contract["服务版本"],
            "返回字段": contract["返回字段"],
            "缓存签名": signature,
            "请求参数": norm_params,
            "响应数据": data,
            "数据集来源": f"{DATASET_MODULE}:{DATASET_PUBLISHED_STATUS}",
            "数据集行数": len(dataset),
            "生成时间": _now(),
        }
        rows.append(snapshot)
        return snapshot, (
            f"已按契约 v{contract['契约版本']} 冻结的返回字段生成响应数据快照，"
            f"缓存签名 {signature}"
        ), False

    def list_snapshots(
        self,
        *,
        signature: str | None = None,
        path: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(SNAPSHOT_MODULE)
        if signature:
            rows = [row for row in rows if signature in str(row.get("缓存签名", ""))]
        if path:
            rows = [row for row in rows if path in str(row.get("接口路径", ""))]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def collect_published_snapshots(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """大盘发布/刷新的取数入口：只读取已上线服务的当前契约，同步消费响应快照。

        返回（已消费快照引用, 被跳过服务）；已下线服务不再返回新结果，只登记跳过原因。
        这里只读消费，不修改契约与快照，职责仍归数据服务模块。
        """
        consumed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for entry in store.rows(MODULE):
            status = entry.get("status")
            if status != "已上线":
                if status == "已下线":
                    skipped.append({
                        "服务编号": entry.get("服务编号"),
                        "原因": "服务已下线，不再返回新结果",
                    })
                continue
            snapshot, message, cache_hit = self.query(int(entry["id"]), {})
            if snapshot is None:
                skipped.append({"服务编号": entry.get("服务编号"), "原因": message})
                continue
            contract = self._active_contract(int(entry["id"])) or {}
            consumed.append({
                "快照id": snapshot["id"],
                "服务编号": entry.get("服务编号"),
                "接口路径": contract.get("接口路径", snapshot["接口路径"]),
                "契约版本": contract.get("契约版本", snapshot["契约版本"]),
                "服务版本": contract.get("服务版本", snapshot["服务版本"]),
                "返回字段": snapshot["返回字段"],
                "缓存签名": snapshot["缓存签名"],
                "缓存命中": cache_hit,
            })
        return consumed, skipped
