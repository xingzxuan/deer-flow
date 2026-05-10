# ADR-007 · URL 路由与前端租户化

| 项目 | 内容 |
|---|---|
| 状态 | 草稿（Draft） · 2026-05-09 据审计修订 §1 / §8 / §10 / §11 / §12（Better Auth 假设作废）|
| 决策日期 | TBD |
| 决策者 | 前端 lead + 后端 lead + 产品 |
| 关联 ADR | ADR-001 数据隔离、ADR-004 RBAC、ADR-006 运行时与渠道 |
| 关联审计 | [adr-vs-code-audit](./adr-vs-code-audit.zh-CN.md) |
| 代码命名 | 本 ADR 写 `tenant_id` / JWT `tid`，落代码统一读作 `workspace_id` / JWT `wid`（详 [workspace-schema-design §1, §4](./workspace-schema-design.zh-CN.md)） |

---

## 1. 背景

ADR-001 ~ 006 锁定了数据/沙箱/Key/RBAC/存储/运行时——但客户最先看到的是**浏览器地址栏长什么样**。多租户产品形态决定了 URL 形态、cookie scope、登录跳转、auth 改造方式。

当前 DeerFlow（`frontend/src/`）：
- nginx 把 `/api/*` → Gateway 8001、`/api/langgraph/*` → 同 Gateway（重写）
- 前端**没有 Better Auth**——auth 走后端自签 JWT：Gateway `app/gateway/auth/jwt.py` 签发 → `access_token` cookie（HttpOnly）→ 前端 `core/auth/server.ts:25-26` 读 cookie 调 `/auth/me`
- JWT payload 当前结构：`{sub, exp, iat, ver}`（`auth/jwt.py:14-19`），**无 tid/role**
- `users.token_version` 列已存在 + JWT `ver` claim 已用作失效（`persistence/user/model.py:49`）
- CSRF 双重 cookie 已实现（`csrf_middleware.py` + 前端 `core/api/api-client.ts:20-32`）
- 没有租户概念，单 host 单工作区
- LangGraph SDK client 在 `core/api/api-client.ts` 单例，所有 thread 操作共用一个 SDK 实例

多租户后必须回答：

1. URL 怎么标记 tenant？
2. 多 tenant 切换时 SDK 单例怎么处理？
3. cookie 怎么 scope（避免跨 tenant session 串）？
4. JWT 怎么扩展才能带 tenant_id 和 role？

> 审计纠正：原稿假设"前端用 Better Auth 走 cookie session"。`frontend/package.json` 实际不依赖 `better-auth`（grep 命中 0 次）；前端 auth 是后端自签 JWT + `access_token` cookie 直通——所有"Better Auth 改造"段落都改为"扩展现有 `auth/jwt.py` `TokenPayload`"，工作量更小。

---

## 2. 决策

**采用 path-based slug + 顶层 `TenantProvider` + 切换时强制刷新**。

| 维度 | 决策 |
|---|---|
| URL 形态 | `/{tenant_slug}/...`（如 `/acme/threads/abc-123`） |
| 子域名（如 `acme.deerflow.app`） | 推迟到 v2，通过 `tenants.custom_domain` 列预留 |
| Tenant 解析点 | nginx 不解析；后端 `AuthMiddleware` 从 path + JWT 双向交叉校验 |
| Cookie scope | `Path=/`、不绑 tenant；通过 JWT 内 `tid` 区分 |
| SDK 单例 | 全局单例，但切租户时 `await invalidate()` + 强制 reload |
| Auth | 沿用现有 `app/gateway/auth/jwt.py` 自签 JWT；扩 `TokenPayload` 加 `tid` + `role` 字段、bump `ver` 触发旧 token 失效 |

---

## 3. 备选方案与拒绝理由

### A. 子域名（`acme.deerflow.app`）作为默认

**拒绝（默认）。** 几个硬伤：

- **本地开发劝退**：每个开发者要起 `*.localtest.me` 之类的通配 DNS，Docker compose 的 nginx 也要改
- **TLS 证书**：通配证书或 ACME 动态签证；自部署客户卡这一步
- **`access_token` cookie 跨子域**：要走 `Domain=.deerflow.app`，scope 太宽，租户隔离反而变弱
- **CSRF 双重 cookie**：当前 csrf_middleware 假定同源；跨子域要改写

**保留**作为 enterprise plan 的"自定义域名"功能（vanity domain），通过 `tenants.custom_domain` 解析回平台 tenant，但不作为默认。

### B. Header `X-Tenant-Id`（无 URL 标记）

**拒绝。** 浏览器分享一个 thread URL 别人打不开（缺 header），UX 灾难；爬虫/SEO 也无法索引租户公开内容。

### C. URL 不带 tenant，全靠 session

**拒绝。** 用户多 tenant 切换后，浏览器 history 不可区分；同一个 URL 在不同会话里显示不同内容，BUG 报告噩梦。

---

## 4. URL 形态规范

```
公开（不带租户）：
  /                       → 营销页
  /login                  → 登录页
  /signup
  /accept-invite/{token}
  /pricing

租户内：
  /{slug}/                → 租户首页（threads 列表）
  /{slug}/threads/{tid}
  /{slug}/skills
  /{slug}/mcp
  /{slug}/memory
  /{slug}/settings        → 租户设置（owner/admin）
  /{slug}/settings/billing

平台 admin（system_role=platform_admin）：
  /admin/tenants
  /admin/usage
  /admin/audit
```

**slug 约束**：

- `^[a-z0-9](-?[a-z0-9])*$`，3-32 字符
- 保留 slug 黑名单：`admin`、`api`、`auth`、`login`、`signup`、`pricing`、`docs`、`status`、`accept-invite`、`platform`
- 大小写归一化：DB 存小写
- 切换 slug：`tenants` 加 `slug_history` 表，30 天内老 slug 重定向到新 slug，过后 410

API 路径**不带 slug**：

```
/api/v1/...                               # 业务 API（tenant 由 JWT / API key 决定；Stage 1 起强制带版本）
/api/...                                  # 旧路径，Stage 1 起转发到 /api/v1，Stage 3 sunset
/api/langgraph/threads/{tid}/runs/stream  # LangGraph 兼容（不带版本，跟随上游 SDK 约定）
```

理由：API 是 SDK 调用的，不需要人类可读 URL；slug 只在浏览器导航/分享时有意义。

**`/api/v1/` 引入时机与设计**：详见 [headless-api-track §4](../02-rollout/headless-api-track.zh-CN.md#4-核心设计api-版本化) —— Stage 1 切换 mount prefix、保留旧路径转发并加 `X-API-Deprecated` header。

---

## 5. 后端：Path slug 与 JWT 的交叉校验

`AuthMiddleware` 当前从 cookie 读 JWT，注入 `user_id` 到 ContextVar。多租户后改：

```python
async def dispatch(request, call_next):
    if _is_public(request.url.path):
        return await call_next(request)

    payload = await verify_jwt_from_cookie(request)
    jwt_tid: str = payload["tid"]
    role: str = payload["role"]

    # 1. API 调用：tenant 完全靠 JWT
    if request.url.path.startswith("/api/"):
        active_tid = jwt_tid

    # 2. 页面导航：从 path 解析 slug → 反查 tenant_id
    else:
        slug = _extract_slug(request.url.path)
        if slug is None:
            active_tid = jwt_tid
        else:
            tenant = await tenant_repo.get_by_slug(slug)
            if tenant is None:
                raise HTTPException(404, "Tenant not found")
            # JWT tid 与 URL slug 不一致 → 强制重定向到正确 slug 或拒绝
            if tenant.id != jwt_tid:
                # 校验 user 是否是该 tenant 的成员
                membership = await membership_repo.get(tenant.id, payload["sub"])
                if membership is None:
                    raise HTTPException(403, "Not a member of this tenant")
                # 是成员但 JWT 没切过来 → 重定向到 /switch-tenant
                return RedirectResponse(f"/auth/switch-tenant?to={slug}&next={request.url.path}")
            active_tid = tenant.id

    set_current_user(...)
    set_current_tenant(active_tid, role)
    return await call_next(request)
```

**关键**：page 路由用 path slug 校验，API 路由用 JWT tid——两条路只在登录时由 `/auth/switch-tenant` 触发同步。

---

## 6. 前端：TenantProvider + SDK 重建

### 6.1 顶层 Provider

```tsx
// frontend/src/core/tenant/provider.tsx
"use client";

export function TenantProvider({ children, tenantId, slug, role }: Props) {
  const value = useMemo(() => ({ tenantId, slug, role }), [tenantId, slug, role]);
  return <TenantContext.Provider value={value}>{children}</TenantContext.Provider>;
}

export function useTenant() {
  const ctx = useContext(TenantContext);
  if (!ctx) throw new Error("useTenant() outside TenantProvider");
  return ctx;
}
```

挂载点：`app/(tenant)/[slug]/layout.tsx`：

```tsx
export default async function TenantLayout({ params, children }) {
  const { slug } = await params;
  const session = await getSession();
  const tenant = await fetchTenantBySlug(slug);

  if (!tenant) notFound();
  if (session.tid !== tenant.id) {
    // 同上：要么是路径错了，要么是切租户没同步
    redirect(`/auth/switch-tenant?to=${slug}`);
  }

  return (
    <TenantProvider tenantId={tenant.id} slug={slug} role={session.role}>
      {children}
    </TenantProvider>
  );
}
```

### 6.2 LangGraph SDK 实例与 tenant 绑定

当前 `core/api/langgraph-client.ts` 是模块级单例。改造：**SDK 实例**保持单例（HTTP client 不需要重建），但**所有调用 wrapper**强制读 `useTenant()`，把 `tenantId` 作为对话 metadata 传递（实际 tenant 鉴权在后端 JWT，前端传只是为了请求溯源）：

```ts
// useThreadStream.ts
export function useThreadStream(threadId: string) {
  const { tenantId } = useTenant();
  return useStream<ThreadState>(threadId, {
    apiUrl: "/api/langgraph",
    metadata: { tenant_id: tenantId },   // 仅用于日志/追踪，不替代鉴权
  });
}
```

**租户切换时的清理**：
- 切换前用 `cancelAllStreams()` 关掉所有打开的 SSE
- 调用 `POST /api/auth/switch-tenant`（后端重发 JWT 新 cookie）
- 拿到 200 后 `window.location.assign(/{newSlug}/)` 强制硬刷新

**为什么硬刷新**：
- React state 里有大量缓存的 thread / skill / mcp 配置，按租户全洗一遍代码量大
- LangGraph SDK 内部维护 EventSource 连接池，强制重建最干净
- 一次 nav 1-2 秒可接受，远比 in-memory 切租户的边界 bug 划算

### 6.3 租户切换 UI

顶栏组件 `<TenantSwitcher>`：

- 列出 user 的所有 membership（来自 `/api/auth/me` 返回的 `tenants[]`）
- 当前激活租户高亮 + 显著色块（避免误操作）
- 点击切换 → 上面 6.2 的硬刷新流程

### 6.4 路由组与 Server Components

```
frontend/src/app/
├── (marketing)/        # 公开：/, /pricing
├── (auth)/             # 登录注册：/login, /signup, /accept-invite
├── (admin)/            # 平台 admin：/admin/...
└── (tenant)/[slug]/    # 租户内：/{slug}/...
    ├── layout.tsx              # TenantProvider 注入
    ├── page.tsx                # threads 列表
    ├── threads/[tid]/page.tsx
    ├── skills/page.tsx
    ├── settings/page.tsx
    └── ...
```

`layout.tsx` 内的 `fetchTenantBySlug` 走 Server Component → 直连后端，缓存 60s（用 React `cache()`）。

---

## 7. Cookie 与会话

| 维度 | 决策 |
|---|---|
| Session cookie name | `access_token`（沿用现状，HttpOnly） |
| Path scope | `/`（不绑 tenant slug） |
| Domain | 平台主域（不跨子域） |
| SameSite | `Lax`（默认） |
| HttpOnly | 是 |
| Secure | 是（生产） |
| 切换 tenant | 后端**重签**新 JWT，覆写同一 cookie；不清旧 cookie |
| 跨设备登录 | session 多设备 OK；切 tenant 不强制其他设备退出 |

**为什么 cookie 不绑 slug**：用户从 `/acme/...` 切到 `/bigco/...` 时如果 cookie path 不同，会出现"两个 cookie 同时存在浏览器但前端选错一个"的边界 case。统一 path=/ + JWT 内 `tid` 单一来源最干净。

**CSRF**：现有双重 cookie CSRF（`csrf_middleware.py`）保持，CSRF token 不需要按 tenant 区分。

---

## 8. Auth 改造（基于现有自签 JWT）

> 现状：`app/gateway/auth/jwt.py:14-19` `TokenPayload` 当前是 `{sub, exp, iat, ver}`；前端 cookie name 是 `access_token`；`users.token_version` 已存在，bump 该列即让所有旧 JWT 失效。
>
> **代码字段名以 [workspace-schema-design §4](./workspace-schema-design.zh-CN.md#4-jwt-tokenpayload--一次到位的字段集) 为准**：本 ADR 写 `tid`、落代码写 `wid`（同义）。Stage 0 PR2 已锁定 `wid` 命名 + Stage 0 一次性加齐 `wid` + `role` 两字段，避免 Stage 2 再 bump `token_version` 导致全用户重登。

多租户化改造：

1. **扩 `TokenPayload`**（字段名以 workspace-schema-design §4 为准）：
   ```python
   class TokenPayload(BaseModel):
       sub: str           # user_id
       tid: str           # tenant_id（新增；落代码读作 wid / workspace_id）
       role: str          # owner | admin | member（新增）
       exp: int
       iat: int
       ver: int           # bump 即让所有旧 token 失效（沿用）
   ```
   `ver` 字段已经在用——任何 membership 变更（加入/退出/role 调整）都 bump `users.token_version`，下次请求 `access_token` 校验失败强制重新登录。
2. **签发流程**：用户登录成功 → 检查 `tenant_memberships` 数量
   - 0 个：跳到 `/onboarding/create-tenant`（新用户首次登录）
   - 1 个：直接签 `{tid=该 tenant.id, role=membership.role}`，跳 `/{slug}/`
   - 多个：跳到 `/select-tenant` 让用户选；选后签对应 JWT，落 `users.default_tenant_id`
3. **切换 tenant**：`POST /api/auth/switch-tenant` → 校验 membership → 重签 JWT 覆写 `access_token` cookie → 客户端硬刷新（§6.2）
4. **SSO（v2）**：当前自签 JWT 模型可以直接配 SAML / OIDC provider，把外部 IdP 的 user/group 映射到 platform user + membership；不依赖 Better Auth，自由度更高
5. **Invitation 流程**：`/accept-invite/{token}` 路径下点击 → 校验 invitation → 自动 attach membership + bump 用户 `token_version` → 跳到 `/{new_slug}/`

**为什么不引入 Better Auth**：现有 JWT 实现已经有 `token_version` 失效机制 + cookie HttpOnly + CSRF 双重 cookie，扩 2 个字段比引入新 auth 框架的破坏面小得多。引入 Better Auth 反而要重写 `auth_middleware.py` + 前端 `core/auth/` + 所有 server actions 调用——工作量多 1 倍。

---

## 9. 自定义域名（v2 预留）

`tenants` 表加 `custom_domain VARCHAR(253) UNIQUE NULL`。客户配置 CNAME 后：

1. 客户在 settings 里填域名
2. 平台调 ACME 签证（per-domain）+ Caddy/nginx 动态 vhost
3. 请求来时 nginx 看 Host 头：
   - 是平台主域 → 走 path slug 解析
   - 是 custom_domain → 反查 tenant_id 直接注入

不在 v1 范围。

---

## 10. 落地改造清单

| 模块 | 改动 | 估工 |
|---|---|---|
| `tenants.slug` + `slug_history` 表 | DB 迁移 | S |
| `AuthMiddleware` path slug 解析 + 交叉校验 | 后端 | M |
| `/api/auth/switch-tenant` 路由 | 后端 | S |
| `/api/auth/me` 返回 tenant 列表 | 后端 | S |
| `auth/jwt.py` `TokenPayload` 扩 `tid/role` 字段 + 签发流程 | 后端 | S |
| 前端 `app/(tenant)/[slug]/layout.tsx` + Provider | 前端 | M |
| 前端路由全部按 `(tenant)/[slug]/` 重组 | 前端 | L |
| `useTenant()` hook + 所有 API 调用接入 | 前端 | M |
| `<TenantSwitcher>` 组件 | 前端 | S |
| 租户首登 onboarding `/onboarding/create-tenant` | 前端 + 后端 | M |
| Tenant picker 页面 `/select-tenant` | 前端 | S |
| 平台 admin `/admin/...` 路由 + 鉴权 | 前端 + 后端 | M |
| 硬刷新切换流 + cancelAllStreams | 前端 | S |

合计：约 8 人周（前端 5 + 后端 3）。

---

## 11. 风险与缓解

| 风险 | 缓解 |
|---|---|
| slug 冲突（保留字 / 已注册） | 注册流程强制校验黑名单；冲突时返显建议 slug |
| 浏览器分享 URL 给非成员看 | 后端 403，前端展示"申请加入"按钮 |
| 切换 tenant 时 streams 没断干净导致看到上租户的 events | hard reload 兜底；E2E 测试 stream cancellation |
| `TokenPayload` 字段升级导致旧 cookie 校验失败 | 加 fallback：旧 4 字段 token 视为"无 tenant 上下文"，强制走 `/select-tenant` 重发 |
| 自定义域名灰区（DNS / TLS） | v2 才做，v1 不实现 |
| `default_tenant_id` 被删除（成员被踢） | 登录时 fallback 到 memberships 第一个；都没了引导建租户 |
| SEO 收录租户页 | 默认 `noindex`，租户开关启用公开页 |

---

## 12. 推翻条件

- v2 决定走子域名优先 → §2 决策切到子域名 + 兼容老 path 形态 6 个月
- 单页应用改成多页 / SSR 完整迁移 → 前端层重写，路由组结构会变
- 决定改用 Better Auth / Auth.js 等成熟框架 → §8 重写为框架接入路径，但当前评估收益不抵迁移成本

---

## 13. 默认假设

| 项 | 默认 |
|---|---|
| URL 形态 | `/{slug}/...`，slug 3-32 字符小写 |
| 自定义域名 | v2 才支持，v1 不开 |
| Cookie path | `/` |
| Cookie domain | 平台主域，不跨子域 |
| 切换 tenant | 硬刷新（`window.location.assign`） |
| Tenant picker | 1 个 membership 时跳过 |
| API 路径 | 不带 slug（tenant 由 JWT 决定） |
| SEO | 默认 `noindex`，可按租户开 |
