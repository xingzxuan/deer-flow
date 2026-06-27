#!/usr/bin/env python3
"""
HTTP 模式示例：把 DeerFlow 当底层服务，通过 Gateway (REST + SSE) 对话。

运行：
    pip install -r requirements.txt
    python app.py            # 前提：仓库根目录已 `make dev`

字段 / 事件名均已对照后端源码核对：
    鉴权        backend/app/gateway/routers/auth.py + auth_middleware.py + csrf_middleware.py
    线程/运行   backend/app/gateway/routers/threads.py + thread_runs.py
    SSE 事件名  backend/packages/harness/deerflow/runtime/runs/worker.py
"""

import os
import json
import requests

# 默认走 nginx(:2026);只起了 Gateway 时用 BASE=http://localhost:8001 覆盖
BASE = os.environ.get("DF_BASE", "http://localhost:2026")
EMAIL = os.environ.get("DF_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("DF_PASSWORD", "change-me-please-123")  # 至少 8 位，避免弱口令


def authenticate(s: requests.Session) -> None:
    """首启则初始化管理员，否则登录。成功后 cookie 落在 session。"""
    status = s.get(f"{BASE}/api/v1/auth/setup-status").json()
    if status.get("needs_setup"):
        print("→ 首次启动，创建管理员账号")
        r = s.post(f"{BASE}/api/v1/auth/initialize",
                   json={"email": EMAIL, "password": PASSWORD})
    else:
        print("→ 已有账号，登录")
        # login/local 是 OAuth2 表单：字段名 username（填邮箱）+ password
        r = s.post(f"{BASE}/api/v1/auth/login/local",
                   data={"username": EMAIL, "password": PASSWORD})
    r.raise_for_status()
    print("  cookies:", list(s.cookies.keys()))


def _csrf(s: requests.Session) -> dict:
    """双提交 cookie 模式：csrf_token cookie 的值放进 X-CSRF-Token 头。"""
    token = s.cookies.get("csrf_token")
    if not token:
        raise RuntimeError("缺少 csrf_token cookie —— 鉴权可能失败")
    return {"X-CSRF-Token": token}


def create_thread(s: requests.Session) -> str:
    r = s.post(f"{BASE}/api/threads", json={}, headers=_csrf(s))
    r.raise_for_status()
    tid = r.json()["thread_id"]
    print(f"→ 线程已创建: {tid}")
    return tid


_seen_text = ""  # 简单状态，按需扩展为按 message-id 维护


def stream_chat(s: requests.Session, thread_id: str, message: str) -> None:
    body = {
        "assistant_id": "lead_agent",                  # 见 backend/langgraph.json
        "input": {"messages": [{"role": "user", "content": message}]},
        "stream_mode": ["messages-tuple", "values"],   # 增量文本 + 全量状态
    }
    headers = {**_csrf(s), "Accept": "text/event-stream"}

    with s.post(f"{BASE}/api/threads/{thread_id}/runs/stream",
                json=body, headers=headers, stream=True) as resp:
        resp.raise_for_status()
        print(f"\n👤 {message}\n🤖 ", end="", flush=True)

        event, buf = None, []
        for raw in resp.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            line = raw.strip()
            if line == "":                              # 一帧结束
                if event:
                    _handle(event, "\n".join(buf))
                event, buf = None, []
            elif line.startswith(":"):                  # 心跳注释
                continue
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                buf.append(line[5:].strip())
        print()


def _handle(event: str, data: str) -> None:
    if event == "end" or not data:
        return
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return
    if event == "messages":
        # 形如 [chunk_dict, metadata_dict]；AI 文本是增量
        chunk = payload[0] if isinstance(payload, list) and payload else {}
        if chunk.get("type") in ("ai", "AIMessageChunk"):
            content = chunk.get("content")
            text = content if isinstance(content, str) else _flatten(content)
            if text:
                print(text, end="", flush=True)
    # event == "metadata" → {run_id, thread_id}
    # event == "values"   → 全量状态快照（title / messages / artifacts ...）


def _flatten(content) -> str:
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def main() -> None:
    s = requests.Session()
    authenticate(s)
    tid = create_thread(s)
    stream_chat(s, tid, "用一句话介绍你自己，然后心算 17 * 23。")
    stream_chat(s, tid, "刚才结果再乘以 2 是多少？")   # 复用 thread_id 即多轮


if __name__ == "__main__":
    main()
