# ADR-004 · 租户内层级与 RBAC

| 项目 | 内容 |
|---|---|
| 状态 | 草稿（Draft） |
| 决策日期 | TBD |
| 决策者 | 产品 + 后端 lead |
| 关联 ADR | ADR-001 数据隔离、ADR-003 LLM Key 与计费 |

---

## 1. 背景

DeerFlow 当前的角色模型在 `users.system_role` 字段（`backend/packages/harness/deerflow/persistence/user/model.py:33`），只有两级：

- `admin`：首启账户、平台运维
- `user`：普通注册用户

并且 admin 是**全平台级别**的——它能管所有用户。多租户化后，"管理员"概念要拆成两层：

- **平台 admin**：你（运营 DeerFlow SaaS 的人）的运维账号
- **租户内角色**：客户公司内部的角色（owner / admin / member）

需要决定：**租户内要不要分角色？分多细？**

---

## 2. 决策

**采用二级 RBAC**：每个租户内有 `owner` / `admin` / `member` 三种角色。最小化但够用。

| 角色 | 数量 | 核心权限 |
|---|---|---|
| **owner** | 1 个/租户（可转让） | 删除租户、修改计费、转让所有权、管理 admin |
| **admin** | 多个 | 邀请/移除 member、安装/启用 skill、配置 MCP、查看用量 |
| **member** | 多个 | 跑 agent、上传文件、看自己的 thread、看本租户共享 skill |

---

## 3. 备选方案与拒绝理由

### A. 扁平（租户内不分角色）

**拒绝。** 简单但企业客户会立刻不满：

- 没法满足"我让员工用，但不让他们改 skill 配置"的需求
- 没法做 SSO / SCIM 集成（IT 管理员要能批量管成员）
- 客户拉新时（员工增多）会出现"谁负责清理"的问题

### B. 三级以上 RBAC（自定义角色 / 项目级权限）

**拒绝（起步阶段）。** 复杂度爆炸：

- 自定义角色 = 完整的 RBAC engine（角色继承、权限矩阵、UI 编辑器）
- 项目/工作区级权限 = 还要再加一层组织
- 这是 Enterprise 后期需求，第一年用不到

**保留作为后续扩展点**：等"自定义角色"成为销售卡点再做。

### C. 单一 admin（owner = admin = 同一个）

**拒绝。** owner 和 admin 必须分开：

- owner 是"账户所有权"——负责计费和租户存亡
- admin 是"日常管理"——可以授权多个
- 不分开会导致"所有 admin 都能删租户"，运营失控

---

## 4. 权限矩阵

按"资源 × 动作"展开，下表是 MVP 矩阵（可扩展）：

| 资源 | 动作 | owner | admin | member |
|---|---|---|---|---|
| **tenant** | view | ✓ | ✓ | ✓ |
| | update_settings | ✓ | ✓ |  |
| | delete | ✓ |  |  |
| | transfer_ownership | ✓ |  |  |
| **billing** | view | ✓ | ✓ |  |
| | update_payment | ✓ |  |  |
| | manage_byo_key | ✓ | ✓ |  |
| **members** | invite | ✓ | ✓ |  |
| | remove | ✓ | ✓ |  |
| | change_role | ✓ |  |  |
| **threads** | create | ✓ | ✓ | ✓ |
| | read_own | ✓ | ✓ | ✓ |
| | read_others | ✓ | ✓ |  |
| | delete_own | ✓ | ✓ | ✓ |
| | delete_others | ✓ | ✓ |  |
| **skills** | install | ✓ | ✓ |  |
| | enable_disable | ✓ | ✓ |  |
| | use | ✓ | ✓ | ✓ |
| **mcp_servers** | configure | ✓ | ✓ |  |
| | use | ✓ | ✓ | ✓ |
| **custom_agents** | create_for_self | ✓ | ✓ | ✓ |
| | create_shared | ✓ | ✓ |  |
| | edit_others | ✓ | ✓ |  |
| **usage_reports** | view | ✓ | ✓ |  |
| **audit_log** | view | ✓ | ✓ |  |

注意几个设计选择：

- **`threads:read_others`**：默认 admin 能看（合规审计需要），但建议加配置项让租户 owner 可关掉
- **`custom_agents:create_for_self` 给 member**：每个成员可以建私人 agent，但要 admin 才能"共享给整租户"
- **`skills:use` 给 member**：使用 admin 启用的 skill；不能自己装 / 关

---

## 5. 落地影响

### 5.1 数据模型（ADR-005 phase 0 plan 已含）

```sql
-- 1 个用户可属于多个租户
tenant_memberships (
  tenant_id UUID FK,
  user_id   UUID FK,
  role      VARCHAR(16),         -- 'owner' | 'admin' | 'member'
  invited_by UUID,
  joined_at TIMESTAMP,
  PRIMARY KEY (tenant_id, user_id)
)

-- 1 个租户必须有且仅有 1 个 owner
CREATE UNIQUE INDEX idx_one_owner_per_tenant
  ON tenant_memberships (tenant_id) WHERE role = 'owner';

-- 当前用户的"默认租户"（用户登录时落进哪个 tenant context）
ALTER TABLE users ADD COLUMN default_tenant_id UUID;
```

### 5.2 JWT Payload 改造

当前 JWT 只有 `sub: user_id`。改造后：

```json
{
  "sub": "<user_id>",
  "tid": "<tenant_id>",         // 当前激活的租户
  "role": "admin",               // 当前租户内的角色
  "tv": 5,                       // token_version（保留现有撤销机制）
  "iat": ...,
  "exp": ...
}
```

**为什么把 role 放 JWT**：避免每次请求都查 `tenant_memberships` 表；权限变化时通过 `token_version++` 强制踢人重登。

**租户切换**：用户在 UI 切换租户时，调 `POST /api/v1/auth/switch-tenant` → 服务端校验 membership → 重发 JWT（新 tid + role）+ 新 cookie。

#### 5.2.1 token_version 的存储与失效路径

`token_version` 不是 JWT 自带字段——需要在 `users` 表加一列：

```sql
ALTER TABLE users ADD COLUMN token_version INT NOT NULL DEFAULT 0;
```

签发 JWT 时把当前 `token_version` 写进 `tv` claim；验证 JWT 时**只读一次**（不每请求查 DB）：

- 命中**短 cache**（30s LRU，key=user_id）→ 比对 cache 里的 token_version
- cache miss → 查 DB 一次，写入 cache
- DB 里的 `token_version` 与 JWT 内 `tv` 不一致 → 401，cookie 强制失效，重登

谁会 bump `token_version`：

| 触发 | 谁写 |
|---|---|
| Membership 撤销 | `DELETE /tenants/{tid}/members/{uid}` 时 `users.token_version += 1` |
| 角色变更（admin → member 等） | 同上 |
| 主动注销所有设备（"踢出所有会话"） | `POST /auth/sign-out-all` |
| 密码修改 | `POST /auth/change-password` |
| owner 转让冷静期满 | 双方都 bump |

#### 5.2.2 JWT 内 role 与每请求查 DB 的取舍

这两条**只能选一条**，本 ADR 选 **A：JWT 内 role + 30s cache + 敏感操作必查 DB**：

- **常规请求**（read thread、list skills 等）：信 JWT，30s 内可能拿到过期权限——可接受
- **写敏感资源**（删除 thread、修改 billing、邀请成员、安装 skill）：装饰器 `@require_permission(strict=True)` 强制查 `tenant_memberships`，不走 cache
- **撤销/降权后**：bump `token_version` 让 cache miss → 下一次请求 401 → 用户重登拿新 JWT

这等于"读路径乐观、写路径悲观"，与 ADR-001 RLS 是双层兜底（JWT role 错了 RLS 还在 tenant 维度过滤，跨租户绝不会泄露）。

#### 5.2.3 cache 实现要点

```python
# packages/harness/deerflow/persistence/membership/cache.py
class MembershipCache:
    """Per-process LRU, 30s TTL, key = (user_id,) → token_version."""
    _cache: TTLCache = TTLCache(maxsize=10_000, ttl=30)

    async def get_token_version(self, user_id: str) -> int:
        cached = self._cache.get(user_id)
        if cached is not None:
            return cached
        version = await user_repo.get_token_version(user_id)
        self._cache[user_id] = version
        return version

    def invalidate(self, user_id: str) -> None:
        """主动失效——bump token_version 时调用。"""
        self._cache.pop(user_id, None)
```

进程间不共享（K8s 多 pod 时 30s 内可能不一致——可接受，超过 30s 全部 pod 自动同步）。

**禁忌**：把 membership cache 做到 Redis 之类共享层。理由：失效广播复杂、租户隔离混乱、收益小。30s 不一致窗口比这个复杂度划算。

### 5.3 AuthMiddleware 注入两层 ContextVar

```python
async def dispatch(request, call_next):
    if _is_public(request.url.path):
        return await call_next(request)

    user = await get_current_user_from_request(request)
    jwt_payload = await verify_jwt(request)
    tenant_id = jwt_payload["tid"]
    role = jwt_payload["role"]
    jwt_tv = jwt_payload["tv"]

    # 走 cache 比对 token_version：通常不查 DB
    current_tv = await membership_cache.get_token_version(user.id)
    if jwt_tv != current_tv:
        raise HTTPException(401, "Token revoked, please sign in again")

    # ContextVar + request.state 注入
    set_current_user(user)
    set_current_tenant(tenant_id, role)
    request.state.user = user
    request.state.tenant_id = tenant_id
    request.state.role = role

    return await call_next(request)
```

**这里**只比对 `token_version`（30s cache），**不**每请求查 `tenant_memberships`。membership 状态变化通过 §5.2.1 的 bump 机制传导。

敏感写操作另走一层（`@require_permission(strict=True)`）—— 在路由 handler 里查 DB，详见 §5.2.2。

### 5.4 权限装饰器升级

现有 `@require_permission("threads", "read", owner_check=True)` 沿用，但语义升级：

```python
@router.get("/{thread_id}")
@require_auth
@require_permission("threads", "read", owner_check="self_or_admin")
# self_or_admin: 自己拥有的 thread 自由读；他人的需要 admin/owner
async def get_thread(thread_id: str, request: Request):
    ...

@router.delete("/tenants/{tid}/members/{uid}")
@require_auth
@require_permission("members", "remove", owner_check="admin_only", strict=True)
# strict=True: 不走 cache，强制查最新 membership
async def remove_member(...):
    ...
```

`owner_check` 的几种模式：

- `"self"`：必须是该资源的 user_id 创建者
- `"self_or_admin"`：自己 / 当前租户的 admin/owner 都行
- `"admin_only"`：仅 admin/owner
- `"owner_only"`：仅 owner

`strict=True` 何时必加（不走 30s cache，强制查 DB）：

- 任何修改 RBAC 的操作（邀请/移除/改角色/转让）
- 任何修改计费 / billing 的操作
- 删除 thread / 删除 skill / 删除 MCP server
- owner 才能做的危险操作（删租户）

读操作和普通写操作（创建 thread、改自己的 memory）不需要 strict——cache 不一致最多窗口 30s，足够。

### 5.5 邀请流程

```
1. admin 在 UI 输入 email + role → POST /api/v1/tenants/{tid}/invitations
2. 后端写 invitations 表 → 发邮件（链接含 invitation_token）
3. 受邀用户点击：
   a. 已注册 → 直接 attach membership
   b. 未注册 → 跳注册流程，注册成功后 attach
4. 邀请 7 天过期，admin 可重发或撤销
```

`invitations` 表已在 phase 0 plan 设计中。

### 5.6 Owner 转让

owner 是单一的，转让流程要谨慎：

```
1. owner 在 UI 选定新 owner（必须是当前 admin）
2. 系统给原 owner 发确认邮件 + 二次密码确认
3. 24h 冷静期内可撤销
4. 冷静期满 → membership 表事务交换：
     原 owner.role := 'admin'
     新 owner.role := 'owner'
   （单事务 + 唯一索引保证不会出现 2 个 owner）
```

### 5.7 SSO / SCIM 预留

**SSO（SAML / OIDC）**：

- 复用现有 `oauth_provider` / `oauth_id` 字段（`UserRow`）
- 新增 `tenant_sso_configs(tenant_id, provider, idp_url, cert, ...)`
- 用户首次 SSO 登录时，自动 attach 到 IdP 配置的默认 tenant + 默认角色（通常 member）

**SCIM**（自动用户配置 / 撤销）：

- 实现 `/api/scim/v2/Users` + `/api/scim/v2/Groups` 标准接口
- IdP（Okta / Azure AD）push 用户增删 → 自动同步 `tenant_memberships`
- **第一年可不实现**——SSO 已能覆盖大多数企业需求

---

## 6. 与现有 system_role 的关系

| 字段 | 含义 | 是否保留 |
|---|---|---|
| `users.system_role` | **平台级**角色：`platform_admin` / `user` | 保留 |
| `tenant_memberships.role` | **租户内**角色：`owner` / `admin` / `member` | 新增 |

`platform_admin`（DeerFlow 运营人员）可以跨租户操作（结合 ADR-001 的 `BYPASSRLS` role），但操作必须审计。普通用户的 `system_role = 'user'`。

注意 **首次启动**逻辑改动：

- 旧：`/setup` 创建第一个 admin 用户
- 新：`/setup` 创建第一个 `platform_admin` + 同时建一个名为 `default` 的租户，把这个用户设为 owner（兼容老部署）

---

## 7. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 最后一个 owner 离职导致租户失控 | 不允许 owner 直接退出，必须先转让；提供平台 admin 强制转让的运维接口（带审计） |
| 权限矩阵越改越复杂 | MVP 矩阵冻结半年；新需求先走"是否扁平角色能解决"评审 |
| JWT 里 role 缓存与 DB 不一致 | membership 撤销时 bump `token_version`，cookie 失效 |
| 跨租户用户切换场景容易误操作 | UI 显著标识当前激活租户（顶栏色块 + 租户名）；危险操作再校验 |
| SSO 接入工作量被低估 | SSO 列入 v2 路线图，明确不阻塞 v1 上线 |

---

## 8. 推翻条件

- **走向自定义角色**：销售反馈明确"我们大客户必须自定义 admin/operator/auditor"——升级到完整 RBAC engine
- **走向项目/工作区**：客户内部需要"多个团队互相不可见"——加 workspace 层（tenant > workspace > user）
- **退回扁平**：极简产品形态变化，弃用企业销售路线（不太可能）

---

## 9. 默认假设

| 项 | 默认 |
|---|---|
| 角色数 | 3 (owner/admin/member) |
| Owner 数 | 严格 1 个/租户 |
| Admin 数 | 不限（按 plan 可设上限） |
| 跨租户成员 | 同一 user 可属于多个租户 |
| JWT role claim | 缓存到 token 里，membership 变更走 token_version 失效 |
| 默认新成员角色 | `member` |
| Invitation TTL | 7 天 |
| Owner 转让冷静期 | 24h |
| SSO | v2 路线图（非 v1 阻塞） |
| SCIM | 暂不实现 |
| 自定义角色 | 暂不实现 |
