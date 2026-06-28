#!/usr/bin/env python3
"""
Headless 多租户测试：以 **API Key（Authorization: Bearer dfk_...）** 的 server-to-server
方式跑多租户并发对话并验证隔离 —— 即 [multi_tenant.py] 的「无人值守 / 业务后端」版。

与 multi_tenant.py（浏览器式 cookie + CSRF）的区别：
    - 每个租户先由一个 **人类 owner**（cookie 会话）创建 service account 并 mint 一把
      workspace-scoped API key（plaintext 仅返回一次）；
    - 之后所有对话只用 **Bearer key**（独立 Session、不带任何 cookie / CSRF），
      模拟业务系统 backend 直连 Gateway。

运行（前提：仓库根已起 Gateway，如 `./scripts/dev-gateway.sh start`）：
    DF_BASE=http://localhost:8001 DF_TENANTS=4 \
        uv run --no-project --with requests python multi_tenant_headless.py
    # 没有 uv 时：pip install -r requirements.txt && python multi_tenant_headless.py
环境变量：DF_BASE（网关地址，默认 :8001）、DF_TENANTS（并发租户数，默认 3）、
         DF_TURNS（每租户链式对话轮数，默认 10）、DF_EXTRA=0 可跳过 scope/撤销专项检查。

字段 / 端点（已对照 Stage 1 headless-api 实现）：
    人类鉴权   POST /api/v1/auth/{register,me}                （cookie + CSRF）
    建 SA      POST /api/v1/service-accounts                  （owner cookie + CSRF）
    mint key   POST /api/v1/api-keys → {plaintext, key_prefix, id, ...}（仅此一次返 plaintext）
    撤销 key   DELETE /api/v1/api-keys/{id} → 204
    对话       POST /api/v1/threads ；/threads/search ；/threads/{id}/runs/stream（Bearer，无 CSRF）

校验信号（来自实现）：
    - Bearer 路径下 thread 归属 user_id = service_account.id + workspace_id，与真人同构 → 隔离一致
    - 跨 workspace 访问线程返回 404（藏存在性，非 403）
    - key.scopes 经 AuthContext.permissions 灌入 @require_permission：缺 runs:create → stream 403
    - 撤销后的 key → 401
"""

import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

BASE = os.environ.get("DF_BASE", "http://localhost:8001")
N_TENANTS = int(os.environ.get("DF_TENANTS", "3"))
N_TURNS = int(os.environ.get("DF_TURNS", "10"))  # 每租户的对话轮数
EXTRA_CHECKS = os.environ.get("DF_EXTRA", "1") != "0"  # scope / 撤销专项检查
RUN_ID = uuid.uuid4().hex[:8]
PASSWORD = "DeerTenant-7x9q!"  # ≥8 位且不在弱口令黑名单

# 一把「全权」key 的 scope —— 覆盖 chat 链路需要的 runs:create / threads:read 等。
FULL_SCOPES = "threads:read,threads:write,threads:delete,runs:create,runs:read,runs:cancel"

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# ── 人类 owner 侧（cookie + CSRF）：注册 → 建 SA → mint key ─────────────
def _csrf(s: requests.Session) -> dict:
    token = s.cookies.get("csrf_token")
    if not token:
        raise RuntimeError("缺少 csrf_token cookie —— 鉴权可能失败")
    return {"X-CSRF-Token": token}


def register(s: requests.Session, email: str) -> dict:
    r = s.post(f"{BASE}/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    r.raise_for_status()
    return r.json()  # {id, email, system_role}


def whoami(s: requests.Session) -> dict:
    r = s.get(f"{BASE}/api/v1/auth/me")
    r.raise_for_status()
    return r.json()  # {id, email, default_workspace_id, workspaces:[...]}


def create_service_account(s: requests.Session, name: str) -> dict:
    r = s.post(f"{BASE}/api/v1/service-accounts", json={"name": name}, headers=_csrf(s))
    r.raise_for_status()
    return r.json()  # {id, workspace_id, name, role, status, ...}


def mint_key(s: requests.Session, sa_id: str, name: str, scopes: str) -> dict:
    r = s.post(
        f"{BASE}/api/v1/api-keys",
        json={"service_account_id": sa_id, "name": name, "scopes": scopes, "env": "live"},
        headers=_csrf(s),
    )
    r.raise_for_status()
    return r.json()  # {id, key_prefix, plaintext, scopes, ...}  ← plaintext 仅此一次


def revoke_key(s: requests.Session, key_id: str) -> int:
    r = s.delete(f"{BASE}/api/v1/api-keys/{key_id}", headers=_csrf(s))
    return r.status_code  # 204 = 成功


# ── 业务后端侧（Bearer key，无 cookie / 无 CSRF）──────────────────────
def bearer_session(plaintext: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {plaintext}"})
    return s


def create_thread(s: requests.Session) -> str:
    r = s.post(f"{BASE}/api/v1/threads", json={})
    r.raise_for_status()
    return r.json()["thread_id"]


def stream_answer(s: requests.Session, thread_id: str, message: str) -> dict:
    """发一条消息，按 message-id 分组收集 AI 增量文本（TitleMiddleware 会另起一条
    AI 消息生成标题，必须按 id 分组，否则正文数字会和标题数字粘连导致误判）。"""
    body = {
        "assistant_id": "lead_agent",
        "input": {"messages": [{"role": "user", "content": message}]},
        "stream_mode": ["messages-tuple", "values"],
    }
    headers = {"Accept": "text/event-stream"}  # Bearer 已在 session.headers
    by_id: dict[str, str] = {}
    with s.post(f"{BASE}/api/v1/threads/{thread_id}/runs/stream", json=body, headers=headers, stream=True) as resp:
        resp.raise_for_status()
        event, buf = None, []
        for raw in resp.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            line = raw.strip()
            if line == "":
                if event == "messages" and buf:
                    _collect(by_id, "\n".join(buf))
                event, buf = None, []
            elif line.startswith(":"):
                continue
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                buf.append(line[5:].strip())
    return by_id  # {message_id: text}


def _collect(by_id: dict, data: str) -> None:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return
    chunk = payload[0] if isinstance(payload, list) and payload else {}
    if chunk.get("type") in ("ai", "AIMessageChunk"):
        content = chunk.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(b.get("text", "") for b in content if isinstance(b, dict))
        else:
            text = ""
        mid = chunk.get("id") or "_"
        by_id[mid] = by_id.get(mid, "") + text


def search_threads(s: requests.Session) -> list:
    r = s.post(f"{BASE}/api/v1/threads/search", json={"limit": 100, "offset": 0})
    r.raise_for_status()
    return r.json()  # bare array of ThreadResponse


def get_thread_status(s: requests.Session, thread_id: str) -> int:
    return s.get(f"{BASE}/api/v1/threads/{thread_id}").status_code


def _contains(by_id: dict, n: int) -> bool:
    return any(str(n) in re.findall(r"\d+", text.replace(",", "")) for text in by_id.values())


def _main_text(by_id: dict) -> str:
    return (max(by_id.values(), key=len) if by_id else "").strip()


# ── provisioning：人类 owner 建租户 + SA + key（顺序，cookie 侧）──────────
def provision_tenant(idx: int) -> dict:
    email = f"htenant-{RUN_ID}-{idx}@example.com"
    owner = requests.Session()
    register(owner, email)
    me = whoami(owner)
    sa = create_service_account(owner, name=f"ci-bot-{idx}")
    key = mint_key(owner, sa["id"], name="prod", scopes=FULL_SCOPES)
    rec = {
        "idx": idx,
        "email": email,
        "owner": owner,
        "workspace_id": me.get("default_workspace_id"),
        "sa_id": sa["id"],
        "sa_role": sa.get("role"),
        "key_id": key["id"],
        "key_prefix": key["key_prefix"],
        "plaintext": key["plaintext"],
        "bearer": bearer_session(key["plaintext"]),
    }
    log(
        f"[租户{idx}] 已开通  ws={str(rec['workspace_id'])[:8]} sa={sa['id'][:8]} "
        f"key_prefix={key['key_prefix']} (sa_role={rec['sa_role']})"
    )
    return rec


# ── 一个租户的并发链路（Bearer key，独立线程）──────────────────────────
def run_tenant(prov: dict) -> dict:
    idx = prov["idx"]
    s = prov["bearer"]
    a, b = 11 + idx, 13 + idx * 2  # 每租户不同算式
    d = 2 + idx                    # 每租户不同步长，坐实无串扰
    expected = [a * b]
    for _ in range(1, N_TURNS):
        expected.append(expected[-1] + d)

    rec = {**prov, "d": d, "expected": expected, "turns": [], "ok": False}
    t0 = time.time()
    try:
        rec["t_start"] = t0
        tid = create_thread(s)
        rec["thread_id"] = tid
        for k in range(N_TURNS):
            if k == 0:
                q = f"只回答最终数字：{a} 乘以 {b} 等于多少？"
            else:
                q = f"把你上一条回答的那个数字再加 {d}，只回答最终数字。"
            by_id = stream_answer(s, tid, q)
            hit = _contains(by_id, expected[k])
            rec["turns"].append({"k": k + 1, "expected": expected[k], "text": _main_text(by_id), "ok": hit})
            mark = "✓" if hit else "✗"
            log(f"[租户{idx}]  T{k + 1:>2}/{N_TURNS} 期望 {expected[k]:>5} → {mark} {rec['turns'][-1]['text'][:24]!r}")
        rec["turns_passed"] = sum(t["ok"] for t in rec["turns"])
        rec["all_turns_ok"] = rec["turns_passed"] == N_TURNS
        rec["context_ok"] = all(t["ok"] for t in rec["turns"][1:])
        rec["t_end"] = time.time()
        rec["ok"] = True
        log(f"[租户{idx}] ✓ 完成 {rec['turns_passed']}/{N_TURNS} 轮")
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {e}"
        log(f"[租户{idx}] ✗ 失败：{rec['error']}")
    return rec


# ── headless 专项：scope 强制 + 撤销（在租户 0 的 owner 上做）──────────
def extra_checks(prov0: dict) -> dict:
    owner = prov0["owner"]
    sa_id = prov0["sa_id"]
    out = {"scope_enforced": None, "revocation_401": None}

    # 1) scope 强制：mint 一把只有 threads:read（无 runs:create）的 key → stream 应 403
    try:
        limited = mint_key(owner, sa_id, name="readonly", scopes="threads:read")
        ls = bearer_session(limited["plaintext"])
        tid = create_thread(ls)  # 建线程不需要 scope（仅鉴权），应成功
        body = {
            "assistant_id": "lead_agent",
            "input": {"messages": [{"role": "user", "content": "hi"}]},
            "stream_mode": ["messages-tuple", "values"],
        }
        r = ls.post(f"{BASE}/api/v1/threads/{tid}/runs/stream", json=body, headers={"Accept": "text/event-stream"})
        out["scope_enforced"] = r.status_code == 403
        log(f"  scope 强制：只读 key 发起 stream → HTTP {r.status_code}（期望 403）{'✓' if out['scope_enforced'] else '✗'}")
        revoke_key(owner, limited["id"])
    except Exception as e:  # noqa: BLE001
        out["scope_error"] = f"{type(e).__name__}: {e}"
        log(f"  scope 强制：检查异常 {out['scope_error']}")

    # 2) 撤销：mint 一把临时 key，验证可用 → 撤销 → 再用应 401
    try:
        tmp = mint_key(owner, sa_id, name="throwaway", scopes=FULL_SCOPES)
        ts = bearer_session(tmp["plaintext"])
        before = ts.post(f"{BASE}/api/v1/threads", json={}).status_code  # 撤销前可建线程
        code = revoke_key(owner, tmp["id"])
        after = ts.post(f"{BASE}/api/v1/threads", json={}).status_code   # 撤销后应 401
        out["revocation_401"] = before in (200, 201) and code == 204 and after == 401
        log(
            f"  撤销：撤销前建线程 HTTP {before} → DELETE {code} → 撤销后 HTTP {after}"
            f"（期望 2xx→204→401）{'✓' if out['revocation_401'] else '✗'}"
        )
    except Exception as e:  # noqa: BLE001
        out["revocation_error"] = f"{type(e).__name__}: {e}"
        log(f"  撤销：检查异常 {out['revocation_error']}")
    return out


def main() -> None:
    log(f"=== Headless 多租户测试 BASE={BASE} 租户数={N_TENANTS} 轮数={N_TURNS} 批次={RUN_ID} ===\n")

    # setup-status 只调用一次（60s 限流）
    try:
        st = requests.get(f"{BASE}/api/v1/auth/setup-status", timeout=5)
        if st.status_code == 200:
            log(f"setup-status: {st.json()}")
            if st.json().get("needs_setup"):
                log("⚠ 系统尚未初始化管理员。请先创建管理员（app.py 首启会建），再跑本测试。")
                return
        else:
            log(f"setup-status: HTTP {st.status_code}（限流则忽略，按已初始化处理）")
    except Exception as e:  # noqa: BLE001
        log(f"setup-status 请求失败：{e}")

    # 1) 顺序开通每个租户（人类 owner 建 SA + mint key）
    log(f"\n── 开通 {N_TENANTS} 个租户（owner cookie → SA → API key）──")
    provs = []
    for i in range(N_TENANTS):
        try:
            provs.append(provision_tenant(i))
        except Exception as e:  # noqa: BLE001
            log(f"[租户{i}] ✗ 开通失败：{type(e).__name__}: {e}")
    if not provs:
        log("没有成功开通的租户，终止。")
        return

    # 2) 并发跑所有租户（只用 Bearer key）
    log(f"\n── 并发启动 {len(provs)} 个租户的 Bearer 对话 ──")
    results = []
    with ThreadPoolExecutor(max_workers=len(provs)) as ex:
        futs = [ex.submit(run_tenant, p) for p in provs]
        for f in as_completed(futs):
            results.append(f.result())
    results.sort(key=lambda r: r["idx"])
    ok = [r for r in results if r.get("ok")]

    # 并发证据：对话时间窗是否重叠
    log("\n── 并发证据（对话时间窗，相对秒）──")
    if ok:
        base_t = min(r["t_start"] for r in ok)
        for r in ok:
            s_off, e_off = r["t_start"] - base_t, r["t_end"] - base_t
            bar = " " * int(s_off * 4) + "█" * max(1, int((e_off - s_off) * 4))
            log(f"  租户{r['idx']}: [{s_off:5.1f}s → {e_off:5.1f}s] {bar}")
        spans = [(r["t_start"], r["t_end"]) for r in ok]
        overlapped = any(a[0] < b[1] and b[0] < a[1] for i, a in enumerate(spans) for b in spans[i + 1 :])
        log(f"  → 存在时间窗重叠（真并发）：{overlapped}")

    # 3) 隔离校验（Bearer key 之间）
    log("\n── 隔离校验（跨租户 Bearer）──")
    iso_pass = True
    own_thread = {r["idx"]: r["thread_id"] for r in ok}
    for r in ok:
        s = r["bearer"]
        mine = {t["thread_id"] for t in search_threads(s)}
        only_own = mine == {r["thread_id"]} if mine else False
        leaked = {own_thread[j] for j in own_thread if j != r["idx"]} & mine
        cross_ok = True
        for j, tid in own_thread.items():
            if j == r["idx"]:
                continue
            code = get_thread_status(s, tid)
            if code != 404:
                cross_ok = False
                log(f"  ✗ 租户{r['idx']} 的 key 访问 租户{j} 的线程返回 {code}（期望 404）")
        if leaked:
            iso_pass = False
            log(f"  ✗ 租户{r['idx']} 的 search 里出现了别人的线程：{leaked}")
        if not cross_ok:
            iso_pass = False
        if only_own and cross_ok and not leaked:
            log(f"  ✓ 租户{r['idx']}：search 仅见己有线程，跨租户 GET 均 404")

    # 4) headless 专项（scope 强制 + 撤销）
    extra = {}
    if EXTRA_CHECKS and ok:
        log("\n── Headless 专项检查（scope 强制 + 撤销）──")
        extra = extra_checks(provs[0])

    # 汇总
    log(f"\n── 汇总（每租户 {N_TURNS} 轮链式对话：T1=a×b，之后每轮 +d）──")
    log(f"{'租户':<6}{'sa_id':<12}{'key_prefix':<20}{'thread':<14}{'步长d':<8}{'通过轮数':<12}{'逐轮':<14}{'状态'}")
    for r in results:
        if r.get("ok"):
            seq = "".join("✓" if t["ok"] else "✗" for t in r["turns"])
            passed = f"{r['turns_passed']}/{N_TURNS}"
            log(f"{r['idx']:<6}{r['sa_id'][:8]:<12}{r['key_prefix']:<20}{r['thread_id'][:10]:<14}{r['d']:<8}{passed:<12}{seq:<14}OK")
        else:
            log(f"{r['idx']:<6}{'-':<12}{'-':<20}{'-':<14}{'-':<8}{'-':<12}{'-':<14}FAIL: {r.get('error')}")

    all_ok = len(ok) == len(provs) and len(provs) == N_TENANTS
    turn1_ok = all(r["turns"][0]["ok"] for r in ok) if ok else False
    context_ok = all(r.get("context_ok") for r in ok) if ok else False
    log("\n=== 结果 ===")
    log(f"  租户全部开通+成功:  {all_ok} ({len(ok)}/{N_TENANTS})")
    log(f"  首轮答复无串扰:     {turn1_ok}")
    log(f"  多轮上下文保持:     {context_ok}  ← 第 2~{N_TURNS} 轮每轮都依赖上一轮结果")
    log(f"  租户隔离(Bearer):   {iso_pass}")
    if EXTRA_CHECKS:
        log(f"  scope 强制(403):    {extra.get('scope_enforced')}  ← 缺 runs:create 的 key 不能 stream")
        log(f"  撤销即失效(401):    {extra.get('revocation_401')}")
    verdict = all_ok and turn1_ok and context_ok and iso_pass
    if EXTRA_CHECKS:
        verdict = verdict and extra.get("scope_enforced") and extra.get("revocation_401")
    log(f"  >>> {'PASS ✅' if verdict else 'FAIL ❌'}")


if __name__ == "__main__":
    main()
