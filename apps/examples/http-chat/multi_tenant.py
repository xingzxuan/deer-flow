#!/usr/bin/env python3
"""
多租户并发测试：以 http-chat 的方式（Gateway REST + SSE），多个租户同时对话，
并验证租户隔离。app.py 的并发 / 多租户版。

运行（前提：仓库根已起 Gateway，如 `./scripts/dev-gateway.sh start`）：
    DF_BASE=http://localhost:8001 DF_TENANTS=4 \
        uv run --no-project --with requests python multi_tenant.py
    # 没有 uv 时：pip install -r requirements.txt && python multi_tenant.py
环境变量：DF_BASE（网关地址，默认 :8001）、DF_TENANTS（并发租户数，默认 3）。

每个注册用户 = 一个独立租户（自带独立 workspace）。并发 = 每租户一个
requests.Session（独立 cookie），放进线程池同时跑。

字段/事件名沿用 apps/examples/http-chat/app.py（已对照后端源码）：
    鉴权   /api/v1/auth/{setup-status,register,me}
    线程   POST /api/threads ；列举 POST /api/threads/search ；单查 GET /api/threads/{id}
    SSE    POST /api/threads/{id}/runs/stream

约束（来自后端源码）：
    - GET /auth/setup-status 限流 1 次/60s/IP  → 整个测试只调用一次
    - POST /auth/login/local 限流 5 次/5min/IP → 本测试用 /register 建新租户，不走 login
    - 跨租户访问线程返回 404（不是 403）→ 即隔离信号
"""

import os
import re
import json
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

BASE = os.environ.get("DF_BASE", "http://localhost:8001")
N_TENANTS = int(os.environ.get("DF_TENANTS", "3"))
N_TURNS = int(os.environ.get("DF_TURNS", "10"))  # 每租户的对话轮数
# 同一批次唯一后缀，避免重复运行时 email 冲突
RUN_ID = uuid.uuid4().hex[:8]
PASSWORD = "DeerTenant-7x9q!"  # ≥8 位且不在弱口令黑名单

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def _csrf(s: requests.Session) -> dict:
    token = s.cookies.get("csrf_token")
    if not token:
        raise RuntimeError("缺少 csrf_token cookie —— 鉴权可能失败")
    return {"X-CSRF-Token": token}


def register(s: requests.Session, email: str) -> dict:
    """注册并自动登录（register 会同时下发 access_token + csrf_token cookie）。"""
    r = s.post(f"{BASE}/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    r.raise_for_status()
    return r.json()  # {id, email, system_role}


def whoami(s: requests.Session) -> dict:
    r = s.get(f"{BASE}/api/v1/auth/me")
    r.raise_for_status()
    return r.json()  # {id, email, default_workspace_id, workspaces:[...]}


def create_thread(s: requests.Session) -> str:
    r = s.post(f"{BASE}/api/threads", json={}, headers=_csrf(s))
    r.raise_for_status()
    return r.json()["thread_id"]


def stream_answer(s: requests.Session, thread_id: str, message: str) -> dict:
    """发一条消息，按 message-id 分组收集 AI 增量文本。

    注意：TitleMiddleware 会另起一条 AI 消息生成线程标题，它和正文答复
    是不同的 message-id。必须按 id 分组，否则正文数字会和标题数字粘连
    （如 180 + "12乘15..." → "18012"），导致校验误判。
    """
    body = {
        "assistant_id": "lead_agent",
        "input": {"messages": [{"role": "user", "content": message}]},
        "stream_mode": ["messages-tuple", "values"],
    }
    headers = {**_csrf(s), "Accept": "text/event-stream"}
    by_id: dict[str, str] = {}
    with s.post(f"{BASE}/api/threads/{thread_id}/runs/stream", json=body, headers=headers, stream=True) as resp:
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
    r = s.post(f"{BASE}/api/threads/search", json={"limit": 100, "offset": 0}, headers=_csrf(s))
    r.raise_for_status()
    return r.json()  # bare array of ThreadResponse


def get_thread_status(s: requests.Session, thread_id: str) -> int:
    return s.get(f"{BASE}/api/threads/{thread_id}").status_code


def _contains(by_id: dict, n: int) -> bool:
    """某条 AI 消息里是否独立出现数字 n（按 message-id 分组比对，避免与标题数字粘连）。"""
    return any(str(n) in re.findall(r"\d+", text.replace(",", "")) for text in by_id.values())


def _main_text(by_id: dict) -> str:
    """取最长的一条 AI 消息当正文（标题通常更短）。"""
    return (max(by_id.values(), key=len) if by_id else "").strip()


# ── 一个租户的完整链路（在独立线程里跑）────────────────────────────────
def run_tenant(idx: int) -> dict:
    """N_TURNS 轮链式对话，复用同一 thread：
    T1 = a×b；之后每轮「把上一条数字再加 d」（d 每租户不同）。
    每轮都必须记得上一轮结果，逐轮校验，验证多轮上下文在并发下各自保持。
    """
    email = f"tenant-{RUN_ID}-{idx}@example.com"
    a, b = 11 + idx, 13 + idx * 2  # 每租户不同算式
    d = 2 + idx                    # 每租户不同步长，进一步坐实无串扰

    # 预先算出每轮期望值
    expected = [a * b]
    for _ in range(1, N_TURNS):
        expected.append(expected[-1] + d)

    s = requests.Session()
    rec = {"idx": idx, "email": email, "d": d, "expected": expected, "turns": [], "ok": False}
    t0 = time.time()
    try:
        user = register(s, email)
        me = whoami(s)
        rec["user_id"] = user["id"]
        rec["workspace_id"] = me.get("default_workspace_id")
        rec["t_start"] = t0
        log(f"[租户{idx}] 注册完成 user={user['id'][:8]} ws={str(rec['workspace_id'])[:8]} d={d} email={email}")

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
        rec["context_ok"] = all(t["ok"] for t in rec["turns"][1:])  # 第 2 轮起依赖上下文
        rec["t_end"] = time.time()
        rec["session"] = s
        rec["ok"] = True
        log(f"[租户{idx}] ✓ 完成 {rec['turns_passed']}/{N_TURNS} 轮")
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {e}"
        log(f"[租户{idx}] ✗ 失败：{rec['error']}")
    return rec


def main() -> None:
    log(f"=== 多租户并发测试 BASE={BASE} 租户数={N_TENANTS} 轮数={N_TURNS} 批次={RUN_ID} ===\n")

    # setup-status 只调用一次（60s 限流）
    try:
        st = requests.get(f"{BASE}/api/v1/auth/setup-status", timeout=5)
        if st.status_code == 200:
            log(f"setup-status: {st.json()}")
            if st.json().get("needs_setup"):
                log("⚠ 系统尚未初始化管理员。请先创建管理员（apps/examples/http-chat/app.py 首启会建），再跑本测试。")
                return
        else:
            log(f"setup-status: HTTP {st.status_code}（限流则忽略，按已初始化处理）")
    except Exception as e:  # noqa: BLE001
        log(f"setup-status 请求失败：{e}")

    # 并发跑所有租户
    log(f"\n── 并发启动 {N_TENANTS} 个租户 ──")
    results = []
    with ThreadPoolExecutor(max_workers=N_TENANTS) as ex:
        futs = [ex.submit(run_tenant, i) for i in range(N_TENANTS)]
        for f in as_completed(futs):
            results.append(f.result())
    results.sort(key=lambda r: r["idx"])

    ok = [r for r in results if r.get("ok")]

    # 并发证据：对话时间窗是否重叠
    log("\n── 并发证据（对话时间窗，相对秒）──")
    if ok:
        base_t = min(r["t_start"] for r in ok)
        for r in ok:
            s_off = r["t_start"] - base_t
            e_off = r["t_end"] - base_t
            bar = " " * int(s_off * 4) + "█" * max(1, int((e_off - s_off) * 4))
            log(f"  租户{r['idx']}: [{s_off:5.1f}s → {e_off:5.1f}s] {bar}")
        spans = [(r["t_start"], r["t_end"]) for r in ok]
        overlapped = any(
            a[0] < b[1] and b[0] < a[1] for i, a in enumerate(spans) for b in spans[i + 1 :]
        )
        log(f"  → 存在时间窗重叠（真并发）：{overlapped}")

    # 隔离校验
    log("\n── 隔离校验 ──")
    iso_pass = True
    own_thread = {r["idx"]: r["thread_id"] for r in ok}
    for r in ok:
        s = r["session"]
        mine = {t["thread_id"] for t in search_threads(s)}
        # 1) search 只含自己的线程
        only_own = mine == {r["thread_id"]} if mine else False
        leaked = {own_thread[j] for j in own_thread if j != r["idx"]} & mine
        # 2) 直接 GET 别人的线程 → 期望 404
        cross_ok = True
        for j, tid in own_thread.items():
            if j == r["idx"]:
                continue
            code = get_thread_status(s, tid)
            if code != 404:
                cross_ok = False
                log(f"  ✗ 租户{r['idx']} 访问 租户{j} 的线程返回 {code}（期望 404）")
        if leaked:
            iso_pass = False
            log(f"  ✗ 租户{r['idx']} 的 search 里出现了别人的线程：{leaked}")
        if not cross_ok:
            iso_pass = False
        if only_own and cross_ok and not leaked:
            log(f"  ✓ 租户{r['idx']}：search 仅见己有线程，跨租户 GET 均 404")

    # 汇总
    log(f"\n── 汇总（每租户 {N_TURNS} 轮链式对话：T1=a×b，之后每轮 +d）──")
    log(f"{'租户':<6}{'user_id':<12}{'thread':<14}{'步长d':<8}{'通过轮数':<12}{'逐轮':<14}{'状态'}")
    for r in results:
        if r.get("ok"):
            seq = "".join("✓" if t["ok"] else "✗" for t in r["turns"])
            passed = f"{r['turns_passed']}/{N_TURNS}"
            log(f"{r['idx']:<6}{r['user_id'][:8]:<12}{r['thread_id'][:10]:<14}{r['d']:<8}{passed:<12}{seq:<14}OK")
        else:
            log(f"{r['idx']:<6}{'-':<12}{'-':<14}{'-':<8}{'-':<12}{'-':<14}FAIL: {r.get('error')}")

    all_ok = len(ok) == N_TENANTS
    turn1_ok = all(r["turns"][0]["ok"] for r in ok)
    context_ok = all(r.get("context_ok") for r in ok)        # 第 2 轮起全对
    all_turns_ok = all(r.get("all_turns_ok") for r in ok)    # N 轮全对
    log("\n=== 结果 ===")
    log(f"  租户全部成功:       {all_ok} ({len(ok)}/{N_TENANTS})")
    log(f"  首轮答复无串扰:     {turn1_ok}")
    log(f"  多轮上下文保持:     {context_ok}  ← 第 2~{N_TURNS} 轮每轮都依赖上一轮结果")
    log(f"  全程 {N_TURNS} 轮全对:    {all_turns_ok}")
    log(f"  租户隔离:           {iso_pass}")
    verdict = all_ok and turn1_ok and context_ok and iso_pass
    log(f"  >>> {'PASS ✅' if verdict else 'FAIL ❌'}")


if __name__ == "__main__":
    main()
