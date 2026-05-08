# ADR-005 · 存储拓扑与持久化策略

| 项目 | 内容 |
|---|---|
| 状态 | 草稿（Draft） |
| 决策日期 | TBD |
| 决策者 | 架构 + 后端 lead + SRE |
| 关联 ADR | ADR-001 数据隔离、ADR-002 沙箱隔离、ADR-006 运行时与渠道 |

---

## 1. 背景

DeerFlow 当前所有持久化数据都在容器/主机的本地文件系统下（`{base_dir}/...`，默认 `./.deer-flow`，可用 `DEER_FLOW_HOME` 覆盖）。这套布局是为"单机/单副本本地开发"设计的——多租户上线后会出现三类问题：

1. **多副本不一致**：K8s 横向扩展后，副本 A 写的 `memory.json` 副本 B 看不到。
2. **容器重建丢数据**：沙箱 pod 销毁、Gateway pod 重启即丢失自定义 skill / agent / memory / 上传 / 产物。
3. **没有备份/灾备**：磁盘损坏即客户数据全失。

现状盘点（详见 `paths.py` 与 `skills/storage/`）：

| 数据 | 现位置 | 容器重建会丢 |
|---|---|---|
| 公共 skills | repo 内 `skills/public/`（镜像里） | 否 |
| 自定义 skills（用户安装） | `skills/custom/`（**全局共享**，非 per-user） | **是** |
| 自定义 agent（SOUL.md + config.yaml） | `{base_dir}/users/{uid}/agents/{name}/` | **是** |
| 长期记忆 | `{base_dir}/users/{uid}/memory.json` | **是** |
| 用户上传 | `{base_dir}/users/{uid}/threads/{tid}/user-data/uploads/` | **是** |
| Agent 工作区（草稿） | `.../user-data/workspace/` | 是（可接受） |
| Agent 产物 | `.../user-data/outputs/` | **是** |
| 配置开关 | `extensions_config.json`（仓库根，全局） | **是** |
| 会话/运行/反馈 | SQLAlchemy 表（DB） | 否 |
| 用户/认证 | `users` 表（DB） | 否 |

---

## 2. 决策

**采用三层存储拓扑**：结构化数据进 Postgres，二进制大对象进对象存储，沙箱本地只保留运行期临时区。**沙箱 pod 因此是真正无状态的**——可被任意调度、滚动升级、销毁重建。

```
┌─────────────────────────────────────────────────────────────┐
│                     Postgres (RLS)                          │
│  ┌────────────────┐  ┌──────────────────┐                  │
│  │ users          │  │ agent_configs    │  ← SOUL.md 入库  │
│  │ tenants        │  │ memory_facts     │  ← memory.json 入库│
│  │ memberships    │  │ memory_context   │                  │
│  │ threads_meta   │  │ tenant_secrets   │  ← API key 加密入库│
│  │ runs           │  │ tenant_skill_state│                 │
│  │ run_events     │  │ tenant_mcp_configs│                 │
│  │ feedback       │  │ tenant_quotas    │                  │
│  └────────────────┘  └──────────────────┘                  │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│              对象存储（S3 / OSS / MinIO）                    │
│  deerflow-data/{tenant_id}/...                             │
│    uploads/{thread_id}/...        ← 客户上传               │
│    threads/{thread_id}/outputs/...← agent 产物             │
│  deerflow-skills/{tenant_id}/                              │
│    {skill_name}-{version}.skill   ← 技能包源              │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│         沙箱 Pod 本地（容器 emptyDir / 临时卷）              │
│   /mnt/user-data/workspace/   ← 中间草稿，run 结束清掉      │
│   /mnt/user-data/uploads/     ← 启动时从 S3 拉，按需        │
│   /mnt/user-data/outputs/     ← 写完后同步到 S3            │
│   /mnt/skills/                ← 启动时按租户 enabled 列表拉│
└─────────────────────────────────────────────────────────────┘
```

### 2.1 各类数据的归属

**A. 进 Postgres（结构化、要查询、要并发更新、不大）**

| 数据 | 表 | 备注 |
|---|---|---|
| 自定义 agent 的 `SOUL.md` + `config.yaml` | `agent_configs(tenant_id, user_id, agent_name, soul_md TEXT, config_yaml TEXT, version, updated_at)` | 文本不大；要并发改、版本回滚 |
| memory.json 中的事实 | `memory_facts(tenant_id, user_id, fact_id, content, category, confidence, created_at, source)` | 要按 confidence/category 查询、去重、流式追加 |
| memory.json 中的上下文 | `memory_context(tenant_id, user_id, work_context, personal_context, top_of_mind, ...)` | 1 行 / (tenant, user) |
| skill 启用状态 | `tenant_skill_state(tenant_id, skill_name, enabled, source)` | 替代全局 `extensions_config.json` |
| MCP 配置 | `tenant_mcp_configs(tenant_id, server_name, transport, url, encrypted_config)` | 替代全局 `extensions_config.json` |
| Tenant secrets（LLM key 等） | `tenant_secrets(tenant_id, key, encrypted_value)` | KMS 加密 |

**B. 进对象存储（大、二进制、写一次读多次、版本化天然）**

| 数据 | Object key | 备注 |
|---|---|---|
| 用户上传文件 | `tenants/{tid}/uploads/{thread_id}/{filename}` | presigned PUT 直传，沙箱按需拉 |
| Agent 产物 | `tenants/{tid}/threads/{thread_id}/outputs/{path}` | `present_files` 触发上传 |
| 技能包 `.skill` | `tenants/{tid}/skills/{skill_name}/{version}.skill` | SHA256 校验，做版本控制 |
| 公共技能包（平台） | `platform/skills/{skill_name}/{version}.skill` | 跨租户共享 |

**C. 不持久化（彻底丢弃）**

| 数据 | 为什么 |
|---|---|
| 沙箱 `workspace/` 中间产物 | agent 的"草稿纸"——半成品脚本、临时调试输出。强行同步浪费带宽 + 增加爆炸半径。让 agent 主动 `present_files` 到 outputs 才同步 |
| 解压后的 skill 目录 | 当本地缓存（LRU）。启动时从 S3 拉 .skill 包解压；缓存命中则跳过 |
| Gateway 进程的本地缓存（mtime 失效那些） | 进程级，不需要持久化 |

---

## 3. 备选方案

### A. 全部进 PVC（共享文件存储 RWX，例如 EFS / NFS）
**拒绝。** 看似最小改动，但：
- 读写竞态依然存在（DeerFlow 的 atomic rename 在 NFS 上行为不一致）
- 大量小文件读写性能差（memory.json 每次写需要 fsync）
- 备份策略复杂（NFS 快照 vs 增量备份）
- 退订清理很慢（递归删大量小文件）

### B. 全部进对象存储（连 metadata 都用 S3）
**拒绝。** 用对象存储模拟文件系统：
- 强一致性差（多数对象存储是 read-after-write，没有 CAS）
- 对小文件高频写延迟太高（每次写 50–200ms）
- 没有事务，跨对象一致性靠应用层补
- 列表操作慢（list-objects 是分页拉取）

### C. 分层：DB（结构化）+ S3（二进制）+ 临时区（中间产物）
**采纳。** 各取所长，与 ADR-001（行级隔离 + RLS）天然契合，并让沙箱 pod 真正无状态。

---

## 4. ObjectStorage 接口草稿

抽象层放在 `backend/packages/harness/deerflow/storage/`。所有调用方写抽象接口，**不直接 import boto3**。

```python
# packages/harness/deerflow/storage/protocol.py

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class ObjectMetadata(BaseModel):
    key: str
    size: int
    etag: str
    content_type: str
    last_modified: datetime
    metadata: dict[str, str] = {}  # 自定义元数据（x-amz-meta-*）


class ObjectNotFound(Exception):
    """对象不存在（幂等删除/读取时由实现转换为此异常或返回 None）。"""


@runtime_checkable
class ObjectStorage(Protocol):
    """租户感知的对象存储抽象。

    实现：
      - LocalObjectStorage    开发/单元测试用，落本地目录
      - S3ObjectStorage       AWS S3 / 兼容接口（OSS / OBS / R2）
      - MinIOObjectStorage    自部署 MinIO（S3 协议）

    Key 命名规范（强约束，便于按 prefix 退订清理）：
      tenants/{tenant_id}/...
      platform/...                  ← 跨租户共享资源（公共技能包）
    """

    # ── 同步 / 异步统一为 async（实现里用 aioboto3 / 自托管 thread pool） ──

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> ObjectMetadata: ...

    async def put_stream(
        self,
        key: str,
        stream: AsyncIterator[bytes],
        *,
        content_type: str = "application/octet-stream",
        content_length: int | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectMetadata: ...

    async def get(self, key: str) -> bytes: ...

    async def get_stream(self, key: str) -> AsyncIterator[bytes]: ...

    async def stat(self, key: str) -> ObjectMetadata | None:
        """读取元数据；不存在返回 None（不抛异常）。"""

    async def exists(self, key: str) -> bool: ...

    async def delete(self, key: str) -> None:
        """幂等删除——不存在视为成功。"""

    async def delete_prefix(self, prefix: str) -> int:
        """按 prefix 批量删除，返回删除数量。

        重要：仅供 tenant 退订 / GC 任务使用。生产实现应：
          - 校验 prefix 必须以 ``tenants/{tenant_id}/`` 开头
          - 限制最大并发与最大删除数（避免一次误删全库）
          - 异步分批，超时可继续
        """

    async def list(
        self,
        prefix: str,
        *,
        limit: int = 1000,
        continuation_token: str | None = None,
    ) -> tuple[list[ObjectMetadata], str | None]:
        """分页列出。第二个返回值是下一页 token，None 表示结束。"""

    async def copy(self, src_key: str, dst_key: str) -> ObjectMetadata: ...

    async def presigned_put_url(
        self,
        key: str,
        *,
        expires_in: int = 3600,
        content_type: str = "application/octet-stream",
        max_size_bytes: int | None = None,
    ) -> str:
        """给客户端生成预签名上传 URL。max_size_bytes 用于 S3 POST policy。"""

    async def presigned_get_url(
        self,
        key: str,
        *,
        expires_in: int = 3600,
        content_disposition: str | None = None,
    ) -> str:
        """给客户端生成预签名下载 URL。

        重要：对 HTML/SVG 等活动内容，调用方必须传入
        content_disposition='attachment; filename="..."'
        以保留当前 Gateway artifacts 路由的 XSS 防护策略。
        """
```

### 4.1 调用边界

```python
# 应用层只 import 抽象，不知道底下是 S3 还是 local
from deerflow.storage import ObjectStorage, get_storage

storage: ObjectStorage = get_storage()        # 单例工厂，按 config.yaml 选实现

# 上传：从 sandbox outputs 同步到 S3
await storage.put_stream(
    f"tenants/{tid}/threads/{thread_id}/outputs/{filename}",
    stream=open_async(local_path),
    content_type=guessed_mime,
)

# 退订：清理整个租户（GC 任务）
await storage.delete_prefix(f"tenants/{tid}/")

# 给前端 presigned upload
url = await storage.presigned_put_url(
    f"tenants/{tid}/uploads/{thread_id}/{filename}",
    expires_in=900,
    content_type=mime,
    max_size_bytes=100 * 1024 * 1024,
)
```

### 4.2 配置

```yaml
# config.yaml
storage:
  use: deerflow.storage.s3:S3ObjectStorage     # 反射加载，沿用现有模式
  bucket: deerflow-data
  region: ap-northeast-1
  endpoint_url: null                            # MinIO 时填自部署地址
  access_key_id: $S3_ACCESS_KEY_ID
  secret_access_key: $S3_SECRET_ACCESS_KEY
  encryption: SSE-KMS                           # SSE-S3 / SSE-KMS / null
  kms_key_id: $S3_KMS_KEY_ID                    # 用 KMS 时填
```

---

## 5. 落地改造点（实施清单）

按优先级排序，每条对应第 1 阶段或第 2 阶段的一个 PR：

### 第 1 阶段（必须）

1. **抽象 `ObjectStorage` 接口 + `LocalObjectStorage` 实现** — 走通端到端，开发/测试用本地目录跑，不阻塞迁移
2. **memory.json 迁库**
   - 新增 `memory_facts` / `memory_context` 表
   - 改写 `agents/memory/storage.py` 用 repository
   - 移除 30s debounce 队列的"合并写"逻辑（DB 层不需要了，保留外部 LLM 抽取的 debounce）
   - 数据迁移脚本：扫现有 `memory.json` 文件 → 入库 → 校验 → 删源文件
3. **agent SOUL/config 迁库**
   - 新增 `agent_configs` + `agent_config_history` 表
   - 改写 `setup_agent` / `update_agent` 工具用 repository
   - 数据迁移脚本：扫 `users/{uid}/agents/` → 入库
4. **`extensions_config.json` 拆库**——这条比看起来大，连锁影响展开如下：

   **a. 新表与仓储**
   - `tenant_skill_state(tenant_id, skill_name, enabled, source, version, updated_at)`
   - `tenant_mcp_configs(tenant_id, server_name, transport, url, encrypted_config, updated_at)`
   - 各自配 repository（仿 `persistence/user/` 的 ContextVar AUTO 模式）

   **b. 替换文件 mtime 热重载机制**
   - 现状：`get_app_config()` + 多处 `_is_cache_stale()`（如 `mcp/cache.py:31`）靠文件 mtime 判失效
   - 改造：
     - `extensions_config.py` 保留为"平台默认开关"的载体（公司 ship 的默认值）
     - 运行时配置全部走 DB；mtime 失效信号改为 `tenant_*_configs.updated_at`（DB 单调递增）
     - 缓存层从模块级单例 → per-tenant LRU（详见 ADR-006 §2.2 / §2.3）

   **c. Gateway 路由改写**
   - `app/gateway/routers/skills.py`：`PUT /skills/{name}/enable`、`POST /skills/install` → 全部改为按当前 tenant_id 写 `tenant_skill_state` / 上传 S3
   - `app/gateway/routers/mcp.py`：`PUT /mcp/servers/{name}` → 改为按当前 tenant_id 写 `tenant_mcp_configs`，写完调 `TenantMCPCache.invalidate(tenant_id)`
   - 鉴权：admin/owner 才能改（参照 ADR-004 §5.4 `strict=True`）

   **d. AppConfig 反射链不动**
   - `config.yaml` 里的 model / sandbox / channels 等仍是平台级配置，不动
   - 只有"per-tenant 可定制的开关"挪到 DB（skill enabled、MCP server、tenant secrets）
   - 旧的 `extensions_config.json` 在迁移完成后**删文件 + 删读取代码**，CI 加禁字典阻止再被引用

   **e. 数据迁移**
   - 写一次性脚本：扫现有 `extensions_config.json` → 写入 "legacy_tenant" 的 `tenant_skill_state` / `tenant_mcp_configs`
   - 校验所有租户 enabled list 与现状一致后，删除文件
   - 自部署单机用户：first-run upgrade 时自动跑此脚本

   **f. 老用户/单机模式兼容**
   - 单机部署 = 1 租户（"default"）；体验上不应感知"DB 化"——配置改完仍立即生效
   - 解决路径：tenant_*_configs 写入后，立即 invalidate 进程内 cache（同一个进程，没问题）
   - 多 pod 部署：靠 `updated_at` 版本号在请求路径上自动同步，无需广播

   **估工**：原 ADR-005 列了 1 条 bullet 偏乐观；这块包含 c/d/e/f 四子项，**整体约 M 偏 L**（一周量级），不是 S。

### 第 1 阶段 / 第 2 阶段交界

5. **`S3ObjectStorage` 实现** — 用 aioboto3，覆盖 protocol 全部方法
6. **上传文件改 presigned 直传**
   - 前端拿 presigned PUT URL → 直传 S3
   - 后端收 "upload complete" 通知 → 异步触发 markitdown 转换 worker
   - 沙箱启动时按需 lazy 拉取（不预拉所有上传）
7. **agent 产物 outputs 同步**
   - `present_files` 工具：上传 outputs 到 S3 + 返回 presigned GET URL（带 `Content-Disposition: attachment` 给 HTML/SVG）
   - sandbox 销毁时本地清理（emptyDir 自然回收）
8. **技能包二进制迁 S3**
   - `POST /api/skills/install`：把 .skill 上传到 S3（带 SHA256 metadata），不再解压到本地全局目录
   - 启动时按 tenant skill list 从 S3 拉 + 校验 + 解压到 LRU 缓存

### 第 2 阶段（建议）

9. **退订 GC**：tenant 标记 deleted 后，30 天定时任务跑 `delete_prefix(f"tenants/{tid}/")` + DB cascade delete
10. **跨区域复制 / CDN**：按客户分布加 region replica 或 CloudFront / OSS 加速域名
11. **审计接入**：每次 storage 操作（put/delete/presigned）记审计日志，带 (tenant_id, user_id, key, op, request_id)

---

## 6. 一致性与失败模式

| 场景 | 行为 | 备注 |
|---|---|---|
| Run 中途崩溃，outputs 没上传完 | DB 里 run 标记为 failed；下次重试 / 用户重新发起 | outputs 是"声明产物"，丢失中间状态可接受 |
| S3 上传成功但 DB 写失败 | 后台 GC 扫"无 DB 引用的 S3 对象"清理 | 标准的 sweep 模式 |
| DB 写成功但 S3 上传失败 | 应用层重试；持续失败则把 run 标 failed 并告警 | 不允许 DB 引用一个不存在的 S3 对象 |
| 客户上传到 presigned URL 但没通知后端 | 后台 sweeper 扫"上传超时未关联 thread 的对象"清理 | TTL 1h |
| Memory 抽取异步任务卡住 | DB 层不会有半成品（事务 commit 才落库） | 保留之前的可观测性 |

---

## 7. 风险与推翻条件

**风险：**
- S3 调用延迟（上传/下载几十 MB 文件耗时，影响 sandbox 冷启动）→ 用 region 同区 + 内网 endpoint 缓解；首启延迟拿 LRU 命中率监控
- presigned URL 泄露（被截获后任何人可上传/下载）→ TTL 短（≤1h）+ `max_size_bytes` 限制 + content-type 限制
- 退订时 `delete_prefix` 误伤 → 强制 prefix 校验 + dry-run 模式 + 二次确认

**推翻条件：**
1. 实测 sandbox 冷启动 P99 > 30s 且无法靠 LRU 缓存解决 → 改用 PVC 存技能包 + 共享只读卷
2. 客户合规要求物理隔离（监管类客户） → 切 ADR-001 的 per-tenant DB 方案，对象存储也切 per-tenant bucket（vs prefix）
3. 团队规模不足以维护 S3 + Postgres 两套基础设施 → 退回 PVC + 单一存储（接受单副本部署）

---

## 8. 默认假设

如无相反证据，按此推进：

| 决策项 | 默认值 |
|---|---|
| 对象存储实现 | S3 兼容接口（生产用 AWS S3 / 阿里 OSS，自部署用 MinIO） |
| 加密 | SSE-KMS，每个租户一个 KMS key alias（可选，起步用 SSE-S3） |
| Bucket 布局 | 单 bucket + tenant prefix（`tenants/{tid}/...`） |
| Presigned URL TTL | 上传 15 min / 下载 1 h |
| LRU 缓存大小 | 沙箱 pod 本地 5 GB（按 image size 调整） |
| 退订保留期 | 软删除 30 天后 GC |
| 技能包大小上限 | 50 MB |
| 单上传文件上限 | 100 MB（presigned policy 强制） |
