# ADR-002 · 沙箱隔离模型

| 项目 | 内容 |
|---|---|
| 状态 | 草稿（Draft） |
| 决策日期 | TBD |
| 决策者 | 安全 + 架构 + SRE |
| 关联 ADR | ADR-001 数据隔离、ADR-005 存储拓扑 |

---

## 0. 概念前提

本 ADR 反复出现 **K8s Namespace + gVisor/Kata 运行时 + NetworkPolicy** 三件套——它们在不同层把租户的代码运行环境关起来，缺一不可。先用一段话讲清楚是什么、防什么、不防什么，再读后面的决策细节会顺很多。

### 0.1 K8s Namespace —— 资源/视图隔离

Kubernetes 的逻辑分区。一个集群里跑多租户，每租户分一个 namespace（如 `tenant-acme`、`tenant-bigco`），其中的 Pod / Service / Secret / ConfigMap 互相看不见。配套：

- **ResourceQuota**：限制 namespace 总用量（CPU、内存、Pod 数、存储）
- **LimitRange**：单 Pod 兜底（默认 request/limit、单 Pod 上限）
- **RBAC**：把租户管理员权限只绑到自己的 namespace

⚠️ **不是安全边界**。namespace 只让你"看不到"，不是"碰不到"。两租户的 Pod 若都跑在默认 `runc` 上、共享同一个 Linux 内核，**任何一个内核 0day 都能让 A 容器逃逸到宿主，进而看到 B 容器**。所以需要下一层。

> 类比：办公楼里不同公司的门禁卡。同事进不了别人的工位，但墙不会自己变厚。

### 0.2 gVisor / Kata —— 内核级隔离

把容器从宿主内核上"再隔一层"。DeerFlow 沙箱要跑租户上传的任意 Python 代码，必须比 runc 更硬。

**gVisor（Google）**
- 用户态实现一个沙箱内核（Sentry），拦截容器所有 syscall 自己模拟，再用极少几个 syscall 跟真内核打交道
- 攻击面：先攻破 Sentry，再攻破真内核——多一道
- 代价：每个 syscall 中转，IO 密集型 workload 慢 ~10-30%
- 集成：Pod spec 写 `runtimeClassName: gvisor`

**Kata Containers**
- 给每个 Pod 起一个轻量虚拟机（QEMU 或 Firecracker 后端），容器跑在 VM 内独立内核里
- 隔离强度 ≈ 真 VM，启动几百毫秒
- 代价：每 Pod 多占 ~50-150 MB 内存、冷启动比 gVisor 慢一点
- 集成：`runtimeClassName: kata-qemu` / `kata-fc`

> 本 ADR 取舍：默认 **gVisor**（性价比平衡），监管/付费档位切 **Kata-Firecracker**（接近 VM 强度，单独定价）。配套 Cosign 镜像签名 + 只读根文件系统 + drop ALL caps 是纵深防御。

### 0.3 NetworkPolicy —— 出/入流量白名单

K8s 原生防火墙，按 namespace / Pod label 控制谁能跟谁通信。**默认拒绝 + 显式放行**是标准姿势：

```yaml
spec:
  podSelector: {}        # 命中 namespace 内所有 Pod
  policyTypes: [Egress]
  egress: []             # 空白名单 = 全部禁止
```

为什么对 DeerFlow 是关键——租户代码可能尝试访问：

| 目标 | 风险 |
|---|---|
| `169.254.169.254`（云元数据） | 偷 IAM 凭据、节点 token，直接拿下集群 |
| 平台内网 DB / Redis | 横向打到其他租户的数据 |
| 其他租户的 namespace IP | 跨租户监听/嗅探 |
| 互联网 C2 服务器 | 数据外泄、挖矿、僵尸网络 |

默认全部拒绝后，仅放行：DNS（CoreDNS）+ 出口走 **Egress Gateway**（Envoy/Squid 做域名白名单，允许 `api.openai.com`、`pypi.org` 等，拒绝其余）。

> 执行靠 CNI 插件（Calico / Cilium）。Cilium 还支持 L7 策略（如"允许 GET /v1/chat/completions、禁 POST /admin"），是更强的备选。

### 0.4 三件套合起来看

```
租户的 Python 代码
        │
        ▼
┌────────────────────────────────────────┐
│ Pod (tenant-acme namespace)            │  ← K8s Namespace：逻辑隔离 + 配额
│  ├─ runtimeClassName: gvisor           │  ← gVisor：内核级攻击面隔离
│  ├─ readOnlyRootFilesystem             │
│  └─ capabilities.drop: ["ALL"]         │
└────────────────────────────────────────┘
        │  egress
        ▼
┌────────────────────────────────────────┐
│ NetworkPolicy: default-deny            │  ← NetworkPolicy：网络层白名单
│  → 仅允许 Egress Gateway / DNS         │
└────────────────────────────────────────┘
        │
        ▼
   Egress Gateway（域名白名单）
```

- 没有 namespace：租户互相能看见对方的资源对象
- 没有 gVisor：一个内核 0day 全集群陪葬
- 没有 NetworkPolicy：租户代码 `curl 169.254.169.254` 就能拿走节点凭据

三层都套上，才是本 ADR 想要的"对抗任意租户代码"的最低防御姿势。

---

## 1. 背景

沙箱是多租户里**爆炸半径最大**的组件：客户的 agent 可以跑任意 bash 命令、读写文件、调用 MCP 工具。如果隔离不够强，一个客户能：

- **读到其他客户的数据**（容器逃逸 / 共享卷误用）
- **薅云元数据**（`curl http://169.254.169.254/...` 偷 IAM 凭证）
- **横向移动**（同 namespace 的其他容器、同节点的 hostNetwork）
- **耗尽资源**（fork bomb、无限循环、磁盘填满）

DeerFlow 现状有两个 sandbox provider：

| Provider | 强度 | 多租户可用 |
|---|---|---|
| `LocalSandboxProvider` | bash/fs **直接落主机**，零隔离 | ❌ 绝对不能用 |
| `AioSandboxProvider` | Docker 容器（社区实现，`packages/harness/deerflow/community/aio_sandbox/`） | ⚠️ 当前配置不够 |

`AioSandboxProvider` 起点不错（每 thread 一个容器、虚拟路径翻译已有），但默认配置缺少多租户必需的几条隔离：默认出网未禁、CPU/内存 limits 未强制、根文件系统未只读、镜像未签名校验。

---

## 2. 决策

**采用 K8s + 强隔离运行时（gVisor 或 Kata Containers）+ NetworkPolicy 默认禁出网 + per-tenant Namespace。**

| 层 | 作用 |
|---|---|
| **K8s Namespace per tenant** | 资源逻辑隔离；NetworkPolicy 起效边界 |
| **gVisor (runsc) 运行时** | 用户态系统调用拦截，容器逃逸到宿主难度大幅提升；性能损失 ~5-15%（多数 agent 任务可接受） |
| **NetworkPolicy 默认 DENY** | 出网白名单：只允许到 LLM endpoint、配置的 MCP servers、搜索 API |
| **ResourceQuota + LimitRange** | per-namespace CPU/内存上限；单 pod CPU/内存上限 |
| **Pod Security Standard: restricted** | 禁 root、禁 privileged、只读根文件系统、drop ALL caps |
| **emptyDir 临时卷** | 数据靠 ADR-005 同步对象存储，pod 销毁即清 |
| **镜像签名校验**（Cosign） | 启动 pod 前验证 sandbox 镜像签名，防供应链攻击 |

**Premium 客户档位**：在此之上再加 per-tenant **物理节点池** + **Firecracker microVM**（kata-fc），把"租户 X 的沙箱永远不和别人共享物理节点"做到 SLA 里。

---

## 3. 威胁模型

| 攻击场景 | 共享 Docker（现状） | per-tenant K8s NS + gVisor（决策） | per-tenant Firecracker |
|---|---|---|---|
| 容器逃逸到宿主 | 全员沦陷 | 单租户沦陷（gVisor 大幅降低成功率） | 单租户沦陷（VM 边界，逃逸难度极高） |
| 容器间横向移动（同节点） | 可行 | NetworkPolicy 禁止 + namespace 隔离 | 不可能（独立 VM） |
| 出网到云元数据 169.254.169.254 | 默认可行 | NetworkPolicy 禁 + IMDSv2 强制 token | 同左 |
| 出网到内部服务（DB / 内网） | 可行 | NetworkPolicy 禁 + egress gateway 白名单 | 同左 |
| 侧信道（CPU 缓存 / Spectre） | 可行 | 减弱（gVisor 隔离系统调用，但 CPU 共享仍有风险） | 显著减弱（独立 VM、独立 vCPU） |
| 资源耗尽（fork bomb / OOM） | 影响同节点全部容器 | LimitRange 强制 cgroup；超限 OOMKill | VM 内独立调度 |
| 持久化攻击（写定时任务） | 可写主机 cron | 只读根文件系统 + ephemeral pod 重建即清 | 同左 |
| 提权 | 看 Docker 配置（默认 root） | restricted PSS 禁 root、禁 capabilities | 同左 |
| 镜像被替换（供应链） | 不校验 | Cosign 验证签名 + admission controller 拦截 | 同左 |

---

## 4. 备选方案与拒绝理由

### A. 共享 Docker（保留 AioSandboxProvider 当前模式）

**拒绝。** 即使加固到极致，根本问题仍在：

- 所有租户共享 dockerd / containerd，一次容器逃逸 = 全员沦陷
- Docker 的 NetworkPolicy 等价物（user-defined network）粒度粗
- 资源限制依赖 cgroup v1/v2 配置一致，运维难统一

### B. per-tenant Firecracker microVM（默认）

**拒绝（作为默认）。** 隔离最强但成本最高：

- 冷启动比 K8s pod 慢 2-5×（500ms vs 几秒）
- 运维需要专门团队（kata-fc / cloud-hypervisor 都不是开箱即用）
- AWS / 阿里云的部分托管 K8s 不直接支持 Firecracker，需要自建 nodepool

**保留作为 premium 档位**：把 Firecracker 当付费 SLA 卖给监管类客户。

### C. 共享 K8s Namespace + 仅靠 NetworkPolicy + cgroup

**拒绝。** namespace 不分隔意味着：

- pod-to-pod 通信默认开放（NetworkPolicy 是白名单制，漏一条就全开）
- ServiceAccount 共享，越权读 secret 风险大
- ResourceQuota 是 namespace 级别，没法精细分配到租户

---

## 5. 落地影响

### 5.1 新建 `K8sSandboxProvider`

```python
# packages/harness/deerflow/sandbox/k8s/provider.py
class K8sSandboxProvider(SandboxProvider):
    """每 thread 一个 Pod，按 tenant 落到对应 Namespace。

    生命周期：
      acquire(thread_id) → 创建 Pod（gVisor runtime, restricted PSS, NetworkPolicy 已挂）
      get(sandbox_id)    → 返回与运行中 Pod 通信的客户端（kubectl exec / WebSocket）
      release(sandbox_id) → delete Pod（emptyDir 自动回收）
    """
```

替换 `LocalSandboxProvider`（开发/测试用）和 `AioSandboxProvider`（保留作为单机部署 fallback）。

### 5.2 K8s 资源（每租户 Namespace 一份）

```yaml
# tenant onboarding 时自动渲染、apply
apiVersion: v1
kind: Namespace
metadata:
  name: tenant-{tenant_id}
  labels:
    pod-security.kubernetes.io/enforce: restricted
    deerflow.io/tenant-id: {tenant_id}
---
apiVersion: v1
kind: ResourceQuota
metadata:
  namespace: tenant-{tenant_id}
spec:
  hard:
    cpu: "16"
    memory: 32Gi
    pods: "20"
    requests.storage: 100Gi
---
apiVersion: v1
kind: LimitRange
metadata:
  namespace: tenant-{tenant_id}
spec:
  limits:
  - type: Container
    default:        { cpu: "500m", memory: 1Gi }
    defaultRequest: { cpu: "100m", memory: 256Mi }
    max:            { cpu: "2",    memory: 4Gi }
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: deny-all-egress
  namespace: tenant-{tenant_id}
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:                          # 仅允许下面这些
  - to:
    - namespaceSelector: { matchLabels: { name: kube-system } }
      podSelector: { matchLabels: { k8s-app: kube-dns } }
    ports: [{ protocol: UDP, port: 53 }]
  - to:                            # 通过 egress gateway 出外网
    - podSelector: { matchLabels: { app: deerflow-egress-gateway } }
```

### 5.3 Egress gateway

放一个集中的出口代理（envoy / squid）做：

- LLM endpoint 白名单（OpenAI / Anthropic / vLLM 内网）
- MCP server 白名单（per-tenant 启用列表）
- 搜索 API 白名单（Tavily / Jina / Brave / DuckDuckGo）
- **黑名单**：169.254.169.254（cloud metadata）、10.0.0.0/8 / 172.16.0.0/12 / 192.168.0.0/16（内部网络，除非白名单）
- 全量审计（按租户记录每次出网）

### 5.4 Pod Spec 关键字段

```yaml
spec:
  runtimeClassName: gvisor              # 或 kata-fc（premium）
  automountServiceAccountToken: false   # 沙箱不该有 SA token
  securityContext:
    runAsNonRoot: true
    runAsUser: 65532
    seccompProfile:
      type: RuntimeDefault
  containers:
  - name: sandbox
    image: registry.deerflow.io/sandbox:v2.3.4@sha256:...   # 强制 digest pin
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities: { drop: [ALL] }
    volumeMounts:
    - { name: workspace, mountPath: /mnt/user-data }
    - { name: skills,    mountPath: /mnt/skills, readOnly: true }
  volumes:
  - { name: workspace, emptyDir: { sizeLimit: 10Gi } }
  - { name: skills,    emptyDir: { sizeLimit: 5Gi } }       # 启动时从 S3 拉
```

### 5.5 镜像签名

- CI 用 cosign sign 给 sandbox 镜像签名
- K8s admission controller（policy-controller / Kyverno）启动 pod 前验证 cosign signature
- 拒绝任何未签名 / 签名不匹配的镜像

### 5.6 SandboxAuditMiddleware 增强

现有 `SandboxAuditMiddleware` 记录了工具调用，多租户必须：

- 每条审计带 `(tenant_id, user_id, thread_id, tool_name, args_hash)`
- 异步推到独立审计存储（不与业务库共用）
- 保留至少 90 天（合规要求常见值）

---

## 6. 性能影响

| 指标 | 估计 |
|---|---|
| Pod 冷启动（gVisor + 镜像 pre-warm） | 1.5-3s |
| Pod 冷启动（Firecracker） | 3-8s |
| Bash 命令延迟（vs 宿主） | gVisor +5-15%；Firecracker +10-30% |
| 文件 I/O（小文件） | gVisor 显著慢（系统调用拦截）；用 emptyDir tmpfs 缓解 |
| 网络（egress gateway） | +1-3ms 单跳 |

**冷启动是最敏感的指标**——靠两条路径优化：

1. **Pod prewarm**：每个 namespace 维护 N 个空闲 pod 池（`PodReadinessProbe` 通过即可服用，按需绑定 thread）
2. **镜像层缓存**：每节点预拉镜像（DaemonSet image-puller）

---

## 7. 风险与缓解

| 风险 | 缓解 |
|---|---|
| gVisor 与某些 syscall 不兼容（agent bash 跑不动某些工具） | sandbox 镜像里预装常用工具；CI 跑兼容性测试集 |
| 出网白名单维护负担 | Per-tenant MCP/搜索配置自动生成 NetworkPolicy；运维 admin UI 一键加 |
| Firecracker 运维复杂 | 仅作为 premium 档位，不强制全量上 |
| 节点 noisy neighbor（CPU 共享导致侧信道） | 高敏租户走专属 nodepool（taints/tolerations） |
| Pod prewarm 池资源浪费 | 按租户活跃度动态调节池大小；闲置超过阈值缩到 0 |
| 镜像供应链攻击 | Cosign 强制 + SBOM + 漏洞扫描 |

---

## 8. 推翻条件

切换到 **per-tenant Firecracker（默认）** 当且仅当：

1. 实测 gVisor 在某条关键 syscall 上有不可绕过的兼容性问题（且 sandbox 镜像无法预装替代品）
2. 拿到合同要求"强物理隔离"的监管客户，付费档位要求覆盖运维成本
3. 出现一次容器逃逸 PoC 影响多租户

切换到 **共享 Docker（极端简化）** 当且仅当：

- 公司决定退回单租户产品形态——多租户上线后基本不应回退

---

## 9. 默认假设

| 项 | 默认 |
|---|---|
| 集群 | EKS / ACK / GKE，K8s 1.29+ |
| 沙箱 runtime | gVisor (runsc) |
| Premium runtime | Kata Containers + Firecracker |
| Pod 隔离粒度 | per-thread（不复用） |
| 冷启动 SLO | P50 < 2s, P99 < 5s |
| Pod CPU/Mem 上限 | 2 CPU / 4 GiB（单 pod） |
| Namespace 配额 | 16 CPU / 32 GiB / 20 pods（按 plan 调） |
| Egress 白名单数量 | <30 个域名 / 租户 |
| 审计保留期 | 90 天热 + 1 年冷 |
| 镜像签名 | cosign + Kyverno 强制 |
