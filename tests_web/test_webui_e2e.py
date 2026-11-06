"""WebUI 平台端到端用例(EdgeC `web/` 重构的验收面)。

判据来源:`web/docs/边缘网关_WebUI平台详细设计文档_v1.6_最终设计基线.md`
  §5.2 权限语义 / §7.1 API 约定 / §7.2 授权映射 / §6.4 配置事务 / §14.1 release-scope。

每条用例都**只断言服务端可见的事实**(状态码、错误码、状态机、摘要),不看界面。
界面上"看起来对了"不算通过——这是这套用例存在的理由。
"""
from __future__ import annotations

import json

import pytest

from testplatform.cases import case

from webui_helpers import EDGEC_WEB, Client, write_evidence

CLAUSE = "free: WebUI 平台设计 v1.6 §7.2 授权映射"


def _record(case_id: str, **fields) -> None:
    write_evidence(case_id, {"caseId": case_id, "clause": CLAUSE, **fields})


@case(id="WUI_00_00_01_000001", clause=CLAUSE, tags=["一致性", "认证"])
def test_anonymous_allowlist_is_exactly_two_endpoints(iems: Client) -> None:
    """匿名只允许 system/info 与 auth/login;能力接口必须要求登录。"""
    info = iems.request("GET", "/api/v1/system/info")
    assert info.status == 200, f"匿名 system/info 应放行,实际 {info.status}"
    assert {"productId", "displayName"} <= set(info.body), (
        f"匿名 system/info 缺必需字段: {sorted(info.body)}"
    )
    assert isinstance(info.body["productId"], str) and info.body["productId"]
    assert isinstance(info.body["displayName"], str) and info.body["displayName"]
    assert set(info.body) <= {"productId", "displayName", "language", "themeVersion", "loginHint"}, (
        f"匿名接口返回了 allowlist 之外的字段: {sorted(info.body)}"
    )

    capabilities = iems.request("GET", "/api/v1/system/capabilities")
    assert capabilities.status == 401, f"未登录读能力快照应 401,实际 {capabilities.status}"
    _record(
        "WUI_00_00_01_000001",
        anonymousFields=sorted(info.body),
        capabilitiesStatus=capabilities.status,
        verdict="product_ok",
    )


@case(id="WUI_00_00_01_000002", clause=CLAUSE, tags=["越权", "负向"])
def test_viewer_cannot_create_user(iems: Client) -> None:
    """viewer 直接打管理接口必须 403,且错误体带稳定错误码与 requestId。"""
    assert iems.login("viewer", "Viewer-Pass-2026").status == 200
    response = iems.request("POST", "/api/v1/users", {"username": "x"})
    assert response.status == 403, f"越权创建用户应 403,实际 {response.status}"
    assert response.code == "permission_denied", f"错误码应为 permission_denied,实际 {response.code}"
    assert response.body.get("requestId"), "错误体缺少 requestId(无法与审计关联)"
    _record("WUI_00_00_01_000002", status=response.status, code=response.code, verdict="product_ok")


@case(id="WUI_00_00_01_000003", clause=CLAUSE, tags=["能力", "负向"])
def test_capability_gate_rejects_cellular_on_iems(iems: Client) -> None:
    """iEMS 台架型号没有蜂窝模块:接口必须 403 capability_unavailable。"""
    assert iems.login("engineer", "Engineer-Pass-2026").status == 200
    response = iems.request("GET", "/api/v1/network/cellular")
    assert response.status == 403, f"能力缺失应 403,实际 {response.status}"
    assert response.code == "capability_unavailable", f"错误码应为 capability_unavailable,实际 {response.code}"
    _record("WUI_00_00_01_000003", status=response.status, code=response.code, verdict="product_ok")


@case(id="WUI_00_00_01_000004", clause="free: WebUI 平台设计 v1.6 §14.1 release-scope 裁剪", tags=["裁剪", "一致性"])
def test_release_scope_removes_upgrade_route_on_iems_but_keeps_on_dcu(iems: Client, dcu: Client) -> None:
    """同一接口在两款产品上的差异,证明裁剪是按 release-scope 生效而不是按代码分支。"""
    assert iems.login("engineer", "Engineer-Pass-2026").status == 200
    iems_response = iems.request("POST", "/api/v1/system/upgrade", {})
    assert iems_response.status == 404, f"iEMS 关闭了升级,路由不应存在,实际 {iems_response.status}"
    assert iems_response.code == "not_found"

    assert dcu.login("engineer", "Engineer-Pass-2026").status == 200
    dcu_response = dcu.request("POST", "/api/v1/system/upgrade", {})
    assert dcu_response.status == 403, f"DCU 启用了升级,应是权限不足而不是路由缺失,实际 {dcu_response.status}"
    assert dcu_response.code == "permission_denied"

    _record(
        "WUI_00_00_01_000004",
        iemsStatus=iems_response.status,
        dcuStatus=dcu_response.status,
        dcuCode=dcu_response.code,
        verdict="product_ok",
    )


@case(id="WUI_00_00_01_000005", clause="free: WebUI 平台设计 v1.6 §5.6 浏览器安全", tags=["安全", "CSRF"])
def test_csrf_and_origin_are_enforced(iems: Client) -> None:
    """状态变更缺 CSRF → 403;伪造 Origin → 403。两者都不能被"正常请求"掩盖。"""
    assert iems.login("viewer", "Viewer-Pass-2026").status == 200

    forged_csrf = iems.request("POST", "/api/v1/auth/logout", {}, headers={"X-CSRF-Token": "forged"})
    assert forged_csrf.status == 403 and forged_csrf.code == "csrf_failed", (
        f"伪造 CSRF 应被拒,实际 {forged_csrf.status}/{forged_csrf.code}"
    )

    forged_origin = iems.request("POST", "/api/v1/auth/logout", {}, headers={"Origin": "http://evil.example"})
    assert forged_origin.status == 403 and forged_origin.code == "origin_not_allowed", (
        f"伪造 Origin 应被拒,实际 {forged_origin.status}/{forged_origin.code}"
    )

    _record(
        "WUI_00_00_01_000005",
        csrfStatus=forged_csrf.status,
        originStatus=forged_origin.status,
        verdict="product_ok",
    )


@case(id="WUI_00_00_01_000006", clause=CLAUSE, tags=["会话", "撤销"])
def test_logout_revokes_session_immediately(iems: Client) -> None:
    """登出后同一 Cookie 再访问必须 401(不是靠前端跳转)。"""
    assert iems.login("viewer", "Viewer-Pass-2026").status == 200
    assert iems.request("GET", "/api/v1/auth/me").status == 200
    assert iems.request("POST", "/api/v1/auth/logout", {}).status in (200, 204)

    after = iems.request("GET", "/api/v1/auth/me")
    assert after.status == 401, f"登出后应 401,实际 {after.status}"
    _record("WUI_00_00_01_000006", afterStatus=after.status, verdict="product_ok")


@case(id="WUI_00_00_01_000007", clause="free: WebUI 平台设计 v1.6 §5.6 首次改密", tags=["认证", "口令"])
def test_first_login_must_change_password(iems: Client) -> None:
    """初始口令登录后 mustChangePassword=true;弱口令被拒;强口令后置为 false。"""
    response = iems.login("admin", "Initial-EdgeOS-2026")
    assert response.status == 200, f"初始口令应可登录,实际 {response.status}"
    assert response.body["user"]["mustChangePassword"] is True, "首登必须标记为需要改密"

    weak = iems.request("POST", "/api/v1/auth/password", {"currentPassword": "Initial-EdgeOS-2026", "newPassword": "short"})
    assert weak.status == 400 and weak.code == "weak_password", f"弱口令应被拒,实际 {weak.status}/{weak.code}"

    strong = iems.request(
        "POST",
        "/api/v1/auth/password",
        {"currentPassword": "Initial-EdgeOS-2026", "newPassword": "Bench-EdgeOS-2026!"},
    )
    assert strong.status in (200, 204), f"强口令应被接受,实际 {strong.status}"

    me = iems.request("GET", "/api/v1/auth/me")
    assert me.body["mustChangePassword"] is False, "改密后必须清除 mustChangePassword"
    _record("WUI_00_00_01_000007", weakStatus=weak.status, finalFlag=me.body["mustChangePassword"], verdict="product_ok")


@case(id="WUI_00_00_01_000008", clause="free: WebUI 平台设计 v1.6 §6.4 配置事务", tags=["事务", "状态机"])
def test_config_transaction_state_machine(iems: Client) -> None:
    """DRAFT → PENDING_CONFIRM → CONFIRMED,且 confirm 幂等。"""
    assert iems.login("engineer", "Engineer-Pass-2026").status == 200

    # 配置分区权限是逐 section 解析的:engineer 有 system.time:write 与 network.interface:write,
    # 但没有 network.firewall:write(安全敏感 R3)
    forbidden = iems.request(
        "POST",
        "/api/v1/config/transactions",
        {"sectionId": "network.firewall", "values": {}},
    )
    assert forbidden.status == 403 and forbidden.code == "permission_denied", (
        f"engineer 不应能改网络接口,实际 {forbidden.status}/{forbidden.code}"
    )

    created = iems.request(
        "POST",
        "/api/v1/config/transactions",
        {"sectionId": "network.interfaces", "values": {"interfaceId": "eth0", "addresses": ["192.168.10.31/24"], "mtu": 1500}},
    )
    assert created.status == 201, f"创建事务应 201,实际 {created.status}"
    transaction_id = created.body["transactionId"]
    assert created.body["state"] == "DRAFT"

    applied = iems.request("POST", f"/api/v1/config/transactions/{transaction_id}/apply", {})
    assert applied.status == 200 and applied.body["state"] == "PENDING_CONFIRM", (
        f"apply 后应进入 PENDING_CONFIRM,实际 {applied.status}/{applied.body.get('state')}"
    )
    assert applied.body.get("deadline"), "R2 事务必须带确认 deadline(回滚由设备侧 watchdog 负责)"

    confirmed = iems.request("POST", f"/api/v1/config/transactions/{transaction_id}/confirm", {})
    assert confirmed.status == 200 and confirmed.body["state"] == "CONFIRMED"

    again = iems.request("POST", f"/api/v1/config/transactions/{transaction_id}/confirm", {})
    assert again.status == 200 and again.body.get("idempotent") is True, "重复 confirm 必须幂等返回"

    _record(
        "WUI_00_00_01_000008",
        transactionId=transaction_id,
        finalState=confirmed.body["state"],
        idempotent=again.body.get("idempotent"),
        verdict="product_ok",
    )


@case(id="WUI_00_00_01_000009", clause="free: WebUI 平台设计 v1.6 §6.4 回滚", tags=["事务", "回滚"])
def test_config_transaction_rollback(iems: Client) -> None:
    """应用后未确认即回滚:状态必须是 ROLLED_BACK,不能停在 PENDING_CONFIRM。"""
    assert iems.login("engineer", "Engineer-Pass-2026").status == 200
    created = iems.request(
        "POST",
        "/api/v1/config/transactions",
        {"sectionId": "network.interfaces", "values": {"interfaceId": "eth0", "addresses": ["192.168.10.32/24"]}},
    )
    transaction_id = created.body["transactionId"]
    iems.request("POST", f"/api/v1/config/transactions/{transaction_id}/apply", {})

    rolled = iems.request("POST", f"/api/v1/config/transactions/{transaction_id}/rollback", {})
    assert rolled.status == 200 and rolled.body["state"] == "ROLLED_BACK", (
        f"回滚后应 ROLLED_BACK,实际 {rolled.status}/{rolled.body.get('state')}"
    )
    _record("WUI_00_00_01_000009", state=rolled.body["state"], verdict="product_ok")


@case(id="WUI_00_00_01_000010", clause="free: WebUI 平台设计 v1.6 §7.1 幂等", tags=["幂等", "任务"])
def test_idempotency_key_returns_same_task_and_conflicts_on_different_body(iems: Client) -> None:
    """同 key 同摘要返回同一 taskId;同 key 不同摘要必须 409。"""
    assert iems.login("engineer", "Engineer-Pass-2026").status == 200
    headers = {"Idempotency-Key": "wui-e2e-1"}

    first = iems.request("POST", "/api/v1/system/services/collector/actions/restart", {}, headers=headers)
    assert first.status == 200, f"首次请求应受理,实际 {first.status}"
    second = iems.request("POST", "/api/v1/system/services/collector/actions/restart", {}, headers=headers)
    assert second.body["taskId"] == first.body["taskId"], "相同幂等键必须复用同一任务"

    conflict = iems.request(
        "POST",
        "/api/v1/system/services/collector/actions/restart",
        {"unexpected": True},
        headers=headers,
    )
    assert conflict.status == 409 and conflict.code == "idempotency_conflict", (
        f"同键不同摘要应 409,实际 {conflict.status}/{conflict.code}"
    )
    _record(
        "WUI_00_00_01_000010",
        taskId=first.body["taskId"],
        conflictStatus=conflict.status,
        verdict="product_ok",
    )


@case(id="WUI_00_00_01_000011", clause="free: WebUI 平台设计 v1.6 §7.5 task-cancel-v1", tags=["任务", "取消"])
def test_task_cancel_requires_owner_or_admin_permission(iems: Client) -> None:
    """engineer 创建的任务,viewer 取消必须 403;创建者自己取消成功。"""
    assert iems.login("engineer", "Engineer-Pass-2026").status == 200
    created = iems.request("POST", "/api/v1/system/services/northbound/actions/restart", {})
    task_id = created.body["taskId"]

    other = Client(iems.base)
    assert other.login("viewer", "Viewer-Pass-2026").status == 200
    denied = other.request("POST", f"/api/v1/tasks/{task_id}/cancel", {})
    assert denied.status == 403, f"非创建者取消应 403,实际 {denied.status}"

    owner = iems.request("POST", f"/api/v1/tasks/{task_id}/cancel", {})
    assert owner.status == 200 and owner.body["state"] == "CANCELED", (
        f"创建者取消应成功,实际 {owner.status}/{owner.body.get('state')}"
    )
    _record(
        "WUI_00_00_01_000011",
        deniedStatus=denied.status,
        ownerState=owner.body["state"],
        verdict="product_ok",
    )


@case(id="WUI_00_00_01_000012", clause="free: WebUI 平台设计 v1.6 §7.4 静态 Registry", tags=["构建", "一致性"])
def test_registry_digest_and_route_table_are_consistent(iems_server, dcu_server) -> None:
    """Registry 是构建产物:摘要必须存在,且接口表与 release-scope 结论一致。"""
    iems = json.loads((EDGEC_WEB / "products/iems/src/generated/registry.json").read_text(encoding="utf-8"))
    dcu = json.loads((EDGEC_WEB / "products/dcu/src/generated/registry.json").read_text(encoding="utf-8"))

    for registry, product in ((iems, "iems"), (dcu, "dcu")):
        assert registry["productId"] == product
        assert registry["digests"]["registry"].startswith("sha256:"), "缺少 Registry 摘要,设备无法校验完整性"
        assert registry["digests"]["releaseScope"].startswith("sha256:")
        for page in registry["pages"]:
            for permission in page["permissions"]["allOf"]:
                assert "*" not in permission, f"运行期 Registry 出现通配符权限: {permission}"

    iems_has_upgrade = any(op["feature"] == "firmwareUpgrade" for op in iems["api"])
    dcu_has_upgrade = any(op["feature"] == "firmwareUpgrade" for op in dcu["api"])
    assert not iems_has_upgrade, "iEMS 关闭了升级,Registry 里不得有升级接口"
    assert dcu_has_upgrade, "DCU 启用了升级,Registry 里应有升级接口"

    _record(
        "WUI_00_00_01_000012",
        iemsDigest=iems["digests"]["registry"][:23],
        dcuDigest=dcu["digests"]["registry"][:23],
        verdict="product_ok",
    )


@case(id="WUI_00_00_01_000013", clause="free: WebUI 平台设计 v1.6 §6.4 风险分类", tags=["事务", "风险"])
def test_r1_section_applies_without_confirm_window(iems: Client) -> None:
    """R1(服务中断)不应弹确认窗口:apply 后直接 CONFIRMED;R2 才需要倒计时。"""
    assert iems.login("engineer", "Engineer-Pass-2026").status == 200
    created = iems.request(
        "POST",
        "/api/v1/config/transactions",
        {"sectionId": "system.time", "values": {"ntpEnabled": True, "servers": ["ntp.aliyun.com"]}},
    )
    assert created.status == 201, f"合法 R1 草稿应创建成功,实际 {created.status}/{created.body}"
    applied = iems.request("POST", f"/api/v1/config/transactions/{created.body['transactionId']}/apply", {})
    assert applied.status == 200
    assert applied.body["state"] == "CONFIRMED", f"R1 不应进入 PENDING_CONFIRM,实际 {applied.body['state']}"
    assert applied.body.get("deadline") in (None, ""), "R1 不应有确认 deadline"
    _record("WUI_00_00_01_000013", state=applied.body["state"], verdict="product_ok")


# ---------------------------------------------------------------- §5.5 账号与会话
@case(id="WUI_00_00_01_000014", clause="free: WebUI 平台设计 v1.6 §5.6 会话管理", tags=["会话", "安全"])
def test_session_revocation_kills_the_other_session(iems: Client) -> None:
    """管理员撤销某条会话后,那条会话的下一次请求必须 401;无权限账号不得列会话。

    判的是"撤销真的生效",不是"接口返回 204"——所以撤销后必须再打一次被撤销的那条会话。
    """
    admin = Client(iems.base)
    assert admin.login("opsadmin", "OpsAdmin-Pass-2026").status == 200

    victim = Client(iems.base)
    assert victim.login("engineer", "Engineer-Pass-2026").status == 200
    assert victim.request("GET", "/api/v1/auth/me").status == 200

    listed = admin.request("GET", "/api/v1/sessions")
    assert listed.status == 200, f"管理员应能列会话,实际 {listed.status}"
    engineer_sessions = [s for s in listed.body["items"] if s["username"] == "engineer"]
    assert engineer_sessions, "会话列表里看不到 engineer 的会话"
    target = engineer_sessions[0]["sid"]

    revoked = admin.request("DELETE", f"/api/v1/sessions/{target}")
    assert revoked.status == 204, f"撤销会话应 204,实际 {revoked.status}/{revoked.body}"

    after = victim.request("GET", "/api/v1/auth/me")
    assert after.status == 401, f"被撤销的会话应立刻失效,实际 {after.status}"

    # 无 user.session:admin 的账号不得列会话(否则等于给了"谁在线"的情报面)
    viewer = Client(iems.base)
    assert viewer.login("viewer", "Viewer-Pass-2026").status == 200
    denied = viewer.request("GET", "/api/v1/sessions")
    assert denied.status == 403, f"viewer 列会话应 403,实际 {denied.status}"
    assert denied.code == "permission_denied"

    _record(
        "WUI_00_00_01_000014",
        revokedSid=target,
        afterStatus=after.status,
        viewerStatus=denied.status,
        verdict="product_ok",
    )


@case(id="WUI_00_00_01_000015", clause="free: WebUI 平台设计 v1.6 §5.5 角色管理", tags=["角色", "负向"])
def test_role_crud_guards_builtin_and_in_use(iems: Client) -> None:
    """自定义角色可改;内置角色不可改;仍被账号持有的角色不可删。"""
    admin = Client(iems.base)
    assert admin.login("opsadmin", "OpsAdmin-Pass-2026").status == 200

    created = admin.request(
        "POST",
        "/api/v1/roles",
        {"id": "wui-crud", "name": "WUI 用例角色", "permissions": ["core.alarm:read"]},
    )
    assert created.status == 201, f"建角色应 201,实际 {created.status}/{created.body}"

    updated = admin.request(
        "PATCH",
        "/api/v1/roles/wui-crud",
        {"name": "WUI 用例角色(改)", "permissions": ["core.alarm:read", "core.telemetry:read"]},
    )
    assert updated.status == 200, f"改自定义角色应 200,实际 {updated.status}/{updated.body}"
    assert updated.body["permissions"] == ["core.alarm:read", "core.telemetry:read"], (
        f"权限集合未按请求更新: {updated.body['permissions']}"
    )

    builtin = admin.request("PATCH", "/api/v1/roles/admin", {"name": "改名试试"})
    assert builtin.status == 403, f"改内置角色应 403,实际 {builtin.status}"
    assert builtin.code == "builtin_role_immutable", f"错误码应为 builtin_role_immutable,实际 {builtin.code}"

    # 有账号持有它时删除必须被拒:否则这些账号会静默失去全部权限
    holder = admin.request("POST", "/api/v1/users", {"username": "wui_role_holder", "roleIds": ["wui-crud"]})
    assert holder.status == 201, f"建持有账号应 201,实际 {holder.status}/{holder.body}"
    in_use = admin.request("DELETE", "/api/v1/roles/wui-crud")
    assert in_use.status == 409, f"删除仍被持有的角色应 409,实际 {in_use.status}"
    assert in_use.code == "role_in_use", f"错误码应为 role_in_use,实际 {in_use.code}"

    _record(
        "WUI_00_00_01_000015",
        updatedPermissions=updated.body["permissions"],
        builtinCode=builtin.code,
        inUseCode=in_use.code,
        verdict="product_ok",
    )


@case(
    id="WUI_00_00_01_000016",
    clause="free: WebUI 平台设计 v1.6 §5.6 登录保护",
    tags=["认证", "负向"],
)
def test_login_lockout_blocks_even_correct_password(iems: Client) -> None:
    """连续失败到阈值后锁定;锁定期间即使口令正确也必须拒绝(否则等于给了校验口)。

    自己建一个一次性账号再打:不依赖种子账号,也不怕同一次会话里重跑(锁定是按
    用户名+IP 记的,拿固定账号会让第二次运行直接撞上锁)。
    """
    admin = Client(iems.base)
    assert admin.login("opsadmin", "OpsAdmin-Pass-2026").status == 200
    account = "wui_lock_case"
    password = "WuiLock-Pass-2026"
    created = admin.request(
        "POST", "/api/v1/users", {"username": account, "password": password, "roleIds": ["viewer"]}
    )
    assert created.status == 201, f"建一次性账号应 201,实际 {created.status}/{created.body}"

    client = Client(iems.base)
    last_failure = None
    for index in range(5):
        response = client.request(
            "POST", "/api/v1/auth/login", {"username": account, "password": f"wrong-{index}"}
        )
        assert response.status == 401, f"第 {index + 1} 次错口令应 401,实际 {response.status}"
        last_failure = response

    locked = client.request("POST", "/api/v1/auth/login", {"username": account, "password": password})
    assert locked.status == 429, f"锁定期间应 429,实际 {locked.status}"
    assert locked.code == "too_many_attempts", f"错误码应为 too_many_attempts,实际 {locked.code}"
    retry_after = locked.body.get("details", {}).get("retryAfterSeconds")
    assert isinstance(retry_after, int) and retry_after > 0, f"应给出可执行的解锁时间,实际 {retry_after}"

    _record(
        "WUI_00_00_01_000016",
        attemptsBeforeLock=5,
        lastFailureStatus=last_failure.status if last_failure else None,
        lockedStatus=locked.status,
        retryAfterSeconds=retry_after,
        verdict="product_ok",
    )


@case(
    id="WUI_00_00_01_000017",
    clause="free: WebUI 平台设计 v1.6 §5.5 授权即时生效",
    tags=["角色", "会话"],
)
def test_permission_change_applies_to_existing_session(iems: Client) -> None:
    """改角色的权限后,已经登录的会话下一次请求就按新权限判定(不做会话缓存)。"""
    admin = Client(iems.base)
    assert admin.login("opsadmin", "OpsAdmin-Pass-2026").status == 200

    assert admin.request(
        "POST",
        "/api/v1/roles",
        {"id": "wui-live", "name": "WUI 即时生效", "permissions": ["core.alarm:read"]},
    ).status == 201
    created = admin.request("POST", "/api/v1/users", {"username": "wui_live", "roleIds": ["wui-live"]})
    assert created.status == 201, f"建账号应 201,实际 {created.status}/{created.body}"
    initial = created.body["initialPassword"]

    member = Client(iems.base)
    login = member.login("wui_live", initial)
    assert login.status == 200, f"新账号应能登录,实际 {login.status}/{login.body}"
    assert member.request("GET", "/api/v1/alarms").status == 200, "改权限前应能看告警"

    # 把 core.alarm:read 换成 core.telemetry:read
    changed = admin.request(
        "PATCH", "/api/v1/roles/wui-live", {"permissions": ["core.telemetry:read"]}
    )
    assert changed.status == 200, f"改权限应 200,实际 {changed.status}/{changed.body}"

    after_alarms = member.request("GET", "/api/v1/alarms")
    after_telemetry = member.request("GET", "/api/v1/telemetry/current")
    assert after_alarms.status == 403, (
        f"权限被移除后同一会话应立刻 403,实际 {after_alarms.status}(说明会话里缓存了旧权限)"
    )
    assert after_telemetry.status == 200, f"新授予的权限应立刻可用,实际 {after_telemetry.status}"

    _record(
        "WUI_00_00_01_000017",
        beforeAlarms=200,
        afterAlarmsStatus=after_alarms.status,
        afterTelemetryStatus=after_telemetry.status,
        verdict="product_ok",
    )


@case(
    id="WUI_00_00_01_000018",
    clause="free: WebUI 平台设计 v1.6 §8.4 远程操作",
    tags=["控制", "负向", "双人复核"],
)
def test_remote_control_requires_two_man(iems: Client) -> None:
    """远程拉合闸:理由必填、发起人不能自批、另一人复核后才改设备状态。

    这是**不可撤销的物理动作**,所以判据不能只看"接口返回 200"——
    必须验证:自批被拒、复核后设备状态真的变了。
    """
    requester = Client(iems.base)
    assert requester.login("engineer", "Engineer-Pass-2026").status == 200
    approver = Client(iems.base)
    assert approver.login("opsadmin", "OpsAdmin-Pass-2026").status == 200

    devices = requester.request("GET", "/api/v1/control/devices")
    assert devices.status == 200, f"读可控设备应 200,实际 {devices.status}"
    closed = [d for d in devices.body["items"] if d["state"] == "closed"]
    assert closed, "台架应有至少一个处于合闸状态的开关"
    target = closed[0]

    # 没有理由的高危操作必须拒绝:事后无法追溯,也挡不住误触
    no_reason = requester.request(
        "POST", "/api/v1/control/commands", {"deviceId": target["id"], "action": "trip", "reason": ""}
    )
    assert no_reason.status == 422, f"缺理由应 422,实际 {no_reason.status}/{no_reason.body}"
    assert no_reason.code == "reason_required", f"错误码应为 reason_required,实际 {no_reason.code}"

    created = requester.request(
        "POST",
        "/api/v1/control/commands",
        {"deviceId": target["id"], "action": "trip", "reason": "现场检修,需要断电"},
    )
    assert created.status == 201, f"发起命令应 201,实际 {created.status}/{created.body}"
    command_id = created.body["id"]
    assert created.body["state"] == "PENDING_APPROVAL", f"应待复核,实际 {created.body['state']}"

    # 双人复核的核心:自己批自己等于没有复核
    self_approve = requester.request("POST", f"/api/v1/control/commands/{command_id}/approve", {})
    assert self_approve.status == 403, f"自批应 403,实际 {self_approve.status}"
    assert self_approve.code == "self_approval_denied", (
        f"错误码应为 self_approval_denied,实际 {self_approve.code}"
    )

    approved = approver.request("POST", f"/api/v1/control/commands/{command_id}/approve", {})
    assert approved.status == 200, f"他人复核应 200,实际 {approved.status}/{approved.body}"
    assert approved.body["state"] == "EXECUTED", f"复核后应 EXECUTED,实际 {approved.body['state']}"

    after = approver.request("GET", "/api/v1/control/devices")
    changed = [d for d in after.body["items"] if d["id"] == target["id"]][0]
    assert changed["state"] == "open", f"复核后设备应分闸,实际 {changed['state']}"

    _record(
        "WUI_00_00_01_000018",
        deviceId=target["id"],
        noReasonStatus=no_reason.status,
        selfApprovalCode=self_approve.code,
        approvedState=approved.body["state"],
        deviceStateAfter=changed["state"],
        verdict="product_ok",
    )


@case(
    id="WUI_00_00_01_000019",
    clause="free: WebUI 平台设计 v1.6 §8.3 告警归并",
    tags=["告警", "归并"],
)
def test_alarm_dedup_and_silence(iems: Client) -> None:
    """同源同码的告警要归并成一条（累加次数）；静默期间只累加、不再新增。

    现场一次故障常常瞬间刷出几十条同码告警，一条条堆着等于没有告警。
    """
    requester = Client(iems.base)
    assert requester.login("engineer", "Engineer-Pass-2026").status == 200
    approver = Client(iems.base)
    assert approver.login("opsadmin", "OpsAdmin-Pass-2026").status == 200

    def control(device_id: str, action: str) -> None:
        created = requester.request(
            "POST",
            "/api/v1/control/commands",
            {"deviceId": device_id, "action": action, "reason": "归并用例：制造同码告警"},
        )
        assert created.status == 201, f"发起 {action} 应 201,实际 {created.status}/{created.body}"
        approved = approver.request(
            "POST", f"/api/v1/control/commands/{created.body['id']}/approve", {}
        )
        assert approved.status == 200, f"复核应 200,实际 {approved.status}/{approved.body}"

    def trip_alarms() -> list[dict]:
        listed = approver.request("GET", "/api/v1/alarms")
        assert listed.status == 200
        return [a for a in listed.body["items"] if a["code"] == "CTL-REMOTE-TRIP"]

    # 先把初始 open 的那个合上，让三个开关都处于合闸态
    devices = requester.request("GET", "/api/v1/control/devices").body["items"]
    for device in devices:
        if device["state"] == "open":
            control(device["id"], "close")
    closed = [d for d in requester.request("GET", "/api/v1/control/devices").body["items"] if d["state"] == "closed"]
    assert len(closed) >= 2, f"台架至少要有两个合闸开关,实际 {len(closed)}"

    # 同一个会话里前面的用例可能已经产生过同码告警(000018 也拉过闸),
    # 所以断言"增量"而不是绝对值 —— 这本身也是归并该有的样子。
    baseline = trip_alarms()
    base_count = baseline[0]["count"] if baseline else 0

    control(closed[0]["id"], "trip")
    first = trip_alarms()
    assert len(first) == 1, f"第一次拉闸应产生 1 条,实际 {len(first)}"
    assert first[0]["count"] == base_count + 1, (
        f"归并计数应为 {base_count + 1},实际 {first[0]['count']}"
    )

    # 静默这条,再拉第二个闸:同码应该只累加次数、不再新增
    silenced = approver.request("POST", f"/api/v1/alarms/{first[0]['id']}/silence", {"durationMinutes": 30})
    assert silenced.status == 200, f"静默应 200,实际 {silenced.status}/{silenced.body}"
    assert silenced.body["state"] == "silenced"
    assert silenced.body.get("silencedUntil"), "静默必须带截止时间"

    control(closed[1]["id"], "trip")
    merged = trip_alarms()
    assert len(merged) == 1, f"同码告警应归并为 1 条,实际 {len(merged)} 条"
    assert merged[0]["count"] == base_count + 2, (
        f"归并后计数应为 {base_count + 2},实际 {merged[0]['count']}"
    )
    assert merged[0]["state"] == "silenced", "静默状态应保持,不能被新事件重置"

    _record(
        "WUI_00_00_01_000019",
        mergedCount=merged[0]["count"],
        state=merged[0]["state"],
        silencedUntil=merged[0]["silencedUntil"],
        verdict="product_ok",
    )


@case(
    id="WUI_00_00_01_000020",
    clause="free: WebUI 平台设计 v1.6 §8.3 通知",
    tags=["告警", "通知"],
)
def test_notification_channel_delivers_new_alarms(iems: Client) -> None:
    """通知渠道：启用的才投递、按最低等级过滤、停用后不再投递。"""
    admin = Client(iems.base)
    assert admin.login("opsadmin", "OpsAdmin-Pass-2026").status == 200

    channels = admin.request("GET", "/api/v1/notifications/channels")
    assert channels.status == 200, f"读渠道应 200,实际 {channels.status}"
    channel = channels.body["items"][0]

    def deliveries() -> list[dict]:
        listed = admin.request("GET", "/api/v1/notifications/deliveries")
        assert listed.status == 200
        return listed.body["items"]

    def rollback_once() -> None:
        """制造一条 CFG-ROLLED-BACK（major）告警。"""
        created = admin.request(
            "POST",
            "/api/v1/config/transactions",
            {"sectionId": "network.interfaces", "values": {"interfaceId": "eth0", "addresses": ["192.168.10.88/24"]}},
        )
        assert created.status == 201, f"建事务应 201,实际 {created.status}/{created.body}"
        applied = admin.request("POST", f"/api/v1/config/transactions/{created.body['transactionId']}/apply", {})
        assert applied.status == 200
        rolled = admin.request("POST", f"/api/v1/config/transactions/{created.body['transactionId']}/rollback", {})
        assert rolled.status == 200

    # 先清台：告警会归并，不清掉旧告警的话这一轮的"新告警"会并进旧的那条，
    # 而通知只在**新产生**的告警上投递（归并进来的不重复推送），断言就无从谈起。
    cleared = admin.request("POST", "/api/v1/alarms/clear", {})
    assert cleared.status == 200, f"清空告警应 200,实际 {cleared.status}/{cleared.body}"

    # 停用状态下：告警不投递
    assert channel["enabled"] is False, "台架初始渠道应为停用,避免污染其它用例"
    before = len(deliveries())
    rollback_once()
    assert len(deliveries()) == before, "渠道停用时不应产生投递"

    # 启用 + 最低等级 major：这次应该投递
    enabled = admin.request(
        "PATCH", f"/api/v1/notifications/channels/{channel['id']}", {"enabled": True, "minLevel": "major"}
    )
    assert enabled.status == 200, f"启用渠道应 200,实际 {enabled.status}/{enabled.body}"
    assert enabled.body["enabled"] is True

    admin.request("POST", "/api/v1/alarms/clear", {})
    before = len(deliveries())
    rollback_once()
    after = deliveries()
    assert len(after) == before + 1, f"启用后应新增 1 条投递,实际 {len(after) - before}"
    assert after[0]["code"] == "CFG-ROLLED-BACK", f"投递的告警码不对: {after[0]['code']}"

    # 把门槛提到 critical：major 的告警就不该再投递
    admin.request("PATCH", f"/api/v1/notifications/channels/{channel['id']}", {"minLevel": "critical"})
    admin.request("POST", "/api/v1/alarms/clear", {})
    before = len(deliveries())
    rollback_once()
    assert len(deliveries()) == before, "低于最低等级的告警不应投递"

    # 收尾：恢复停用，避免影响后续用例
    admin.request("PATCH", f"/api/v1/notifications/channels/{channel['id']}", {"enabled": False})

    _record(
        "WUI_00_00_01_000020",
        channelId=channel["id"],
        deliveredCode=after[0]["code"],
        verdict="product_ok",
    )


@case(
    id="WUI_00_00_01_000021",
    clause="free: WebUI 平台设计 v1.6 §8.3 告警归档",
    tags=["告警", "归档"],
)
def test_alarm_clear_archives_instead_of_dropping(iems: Client) -> None:
    """清台只把告警挪进归档,不能销毁:事后还要能回答"刚才那批是什么"。"""
    admin = Client(iems.base)
    assert admin.login("opsadmin", "OpsAdmin-Pass-2026").status == 200

    def archive_total() -> int:
        response = admin.request("GET", "/api/v1/alarms/archive")
        assert response.status == 200, f"读归档应 200,实际 {response.status}"
        return response.body["total"]

    # 先清台,拿到干净的起点
    assert admin.request("POST", "/api/v1/alarms/clear", {}).status == 200
    before = archive_total()

    created = admin.request(
        "POST",
        "/api/v1/config/transactions",
        {"sectionId": "network.interfaces", "values": {"interfaceId": "eth0", "addresses": ["192.168.10.77/24"]}},
    )
    assert created.status == 201
    tx = created.body["transactionId"]
    assert admin.request("POST", f"/api/v1/config/transactions/{tx}/apply", {}).status == 200
    assert admin.request("POST", f"/api/v1/config/transactions/{tx}/rollback", {}).status == 200

    live = admin.request("GET", "/api/v1/alarms")
    assert len(live.body["items"]) >= 1, "回滚应产生告警"

    cleared = admin.request("POST", "/api/v1/alarms/clear", {})
    assert cleared.status == 200, f"清台应 200,实际 {cleared.status}/{cleared.body}"
    assert cleared.body["archived"] >= 1, f"清台应归档而不是丢弃,实际 archived={cleared.body.get('archived')}"

    assert len(admin.request("GET", "/api/v1/alarms").body["items"]) == 0, "清台后活动列表应为空"
    assert archive_total() == before + cleared.body["archived"], "归档条数应正好增加被清掉的条数"

    archived = admin.request("GET", "/api/v1/alarms/archive").body["items"]
    assert archived[0]["code"] == "CFG-ROLLED-BACK", f"归档里应能看到回滚告警,实际 {archived[0]['code']}"
    assert archived[0].get("archivedAt"), "归档记录必须带归档时间"

    filtered = admin.request("GET", "/api/v1/alarms/archive?code=CFG-ROLLED-BACK").body["items"]
    assert filtered, "按告警码过滤应能查到"
    assert all(item["code"] == "CFG-ROLLED-BACK" for item in filtered), "过滤结果混进了别的告警码"

    _record(
        "WUI_00_00_01_000021",
        archived=cleared.body["archived"],
        archiveTotal=archive_total(),
        verdict="product_ok",
    )
