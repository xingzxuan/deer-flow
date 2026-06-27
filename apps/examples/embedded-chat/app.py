#!/usr/bin/env python3
"""
内嵌模式示例：进程内直接把 DeerFlow 当 SDK 调，不起 HTTP。

必须在 backend 的 uv 环境里跑（这样才能 import deerflow.*）：
    cd backend
    uv run python ../apps/examples/embedded-chat/app.py

依赖 config.yaml 里配好至少一个可用模型 + API key（路径解析见根 CLAUDE.md）。

API 对照：backend/packages/harness/deerflow/client.py
"""

from deerflow.client import DeerFlowClient
from deerflow.runtime.checkpointer.provider import get_checkpointer


def main() -> None:
    # checkpointer 提供跨轮状态持久化（sqlite/postgres 由 config.yaml 决定）
    client = DeerFlowClient(
        checkpointer=get_checkpointer(),
        thinking_enabled=True,
    )

    thread_id = "embedded-demo-1"

    # ① 流式：stream() 产出 StreamEvent
    print("👤 用一句话介绍你自己，然后心算 17 * 23。\n🤖 ", end="", flush=True)
    for ev in client.stream("用一句话介绍你自己，然后心算 17 * 23。", thread_id=thread_id):
        if ev.type == "messages-tuple" and ev.data.get("type") == "ai":
            print(ev.data.get("content", ""), end="", flush=True)   # AI 文本增量
        elif ev.type == "end":
            print(f"\n[usage] {ev.data.get('usage')}")

    # ② 阻塞式：chat() 直接返回完整 AI 文本（复用 thread_id 即多轮）
    print("\n👤 刚才结果再乘以 2 是多少？")
    answer = client.chat("刚才结果再乘以 2 是多少？", thread_id=thread_id)
    print(f"🤖 {answer}")

    # 其它能力：list_models() / list_skills() / get_memory() / upload_files() ...
    models = client.list_models().get("models", [])
    print(f"\n[已配置模型] {[m.get('name') for m in models]}")


if __name__ == "__main__":
    main()
