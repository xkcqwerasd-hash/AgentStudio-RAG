import asyncio
import json
import sys
import httpx

BASE_URL = "http://127.0.0.1:8000/api/v1"


async def main():
    print("=" * 65)
    print("AgentStudio-RAG M2.3 Live Query Rewriting & SSE Verification")
    print("=" * 65)

    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        # 1. Fetch available knowledge bases matching BGE-small-zh-v1.5
        kb_resp = await client.get(f"{BASE_URL}/knowledge-bases")
        assert kb_resp.status_code == 200
        kbs = kb_resp.json()

        target_kb = None
        for kb in kbs:
            if kb.get("chunk_count", 0) > 0 and kb.get("embedding_model") == "BAAI/bge-small-zh-v1.5":
                target_kb = kb
                break
        if not target_kb:
            print("No matching knowledge base found!")
            sys.exit(1)

        print(f"Target Knowledge Base: {target_kb['name']} (ID: {target_kb['id']})")

        # 2. Turn 1: Establish context
        q1 = "请介绍一下知识库中微服务网关的连接池配置规范。"
        print(f"\n[Turn 1 Query]: {q1}")
        t1_payload = {
            "knowledge_base_id": target_kb["id"],
            "query": q1,
            "mode": "workflow",
        }
        t1_resp = await client.post(f"{BASE_URL}/chat/completions", json=t1_payload)
        assert t1_resp.status_code == 200
        t1_data = t1_resp.json()
        conv_id = t1_data["conversation_id"]
        print(f"Turn 1 completed! Conversation ID: {conv_id}")
        print(f"Turn 1 Answer preview: {t1_data['answer'][:80]}...\n")

        # 3. Turn 2: Follow-up question with omission via SSE Stream
        q2 = "那最小是多少？"
        print(f"[Turn 2 Follow-up Query with Omission]: {q2}")
        print("[Connecting to SSE Stream] -> POST /api/v1/chat/completions/stream ...")

        t2_payload = {
            "knowledge_base_id": target_kb["id"],
            "conversation_id": conv_id,
            "query": q2,
            "mode": "workflow",
            "top_k": 4,
        }

        query_rewrite_event = None
        retrieval_event = None
        tokens_received = ""
        trace_event = None

        async with client.stream("POST", f"{BASE_URL}/chat/completions/stream", json=t2_payload) as resp:
            assert resp.status_code == 200
            print(f"Connected! Status: {resp.status_code}, Content-Type: {resp.headers.get('content-type')}\n")

            current_event = "message"
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("event:"):
                    current_event = line.replace("event:", "", 1).strip()
                elif line.startswith("data:"):
                    data_str = line.replace("data:", "", 1).strip()
                    try:
                        d = json.loads(data_str)
                    except Exception:
                        d = data_str

                    if current_event == "query_rewrite":
                        query_rewrite_event = d
                        print(f"[EVENT: query_rewrite]")
                        print(f"  - Original Query : {d.get('original_query')}")
                        print(f"  - Rewritten Query: {d.get('rewritten_query')}")
                        print(f"  - Is Rewritten   : {d.get('is_rewritten')}")
                        print(f"  - Duration       : {d.get('duration_ms')} ms")
                        print(f"  - Tokens Used    : {d.get('tokens_used')}")
                    elif current_event == "retrieval":
                        retrieval_event = d
                        print(f"[EVENT: retrieval] Retrieved {d.get('retrieved_chunks_count')} chunks in {d.get('retrieval_duration_ms')}ms")
                        print("[EVENT: token] Incremental tokens: ", end="", flush=True)
                    elif current_event == "token":
                        delta = d.get("delta", "")
                        tokens_received += delta
                        sys.stdout.write(delta)
                        sys.stdout.flush()
                    elif current_event == "trace":
                        trace_event = d
                    elif current_event == "done":
                        print(f"\n[EVENT: done] Assistant Message ID: {d.get('message_id')}\n")

        # 4. Assertions on events and persistence
        assert query_rewrite_event is not None, "query_rewrite event was not received!"
        assert query_rewrite_event["is_rewritten"] is True, "Query was not rewritten!"
        print(f"Verified query_rewrite event received successfully!")

        # 5. Verify Database Records
        msg_resp = await client.get(f"{BASE_URL}/conversations/{conv_id}/messages")
        assert msg_resp.status_code == 200
        msgs = msg_resp.json()
        print(f"Total messages in Conversation {conv_id}: {len(msgs)}")
        assert len(msgs) >= 4  # Turn 1 (user, assistant) + Turn 2 (user, assistant)

        # Check Turn 2 user message preserves original query
        turn2_user = msgs[-2]
        assert turn2_user["role"] == "user"
        assert turn2_user["content"] == q2, f"Expected '{q2}', got '{turn2_user['content']}'"
        print(f"Verified User Message in DB preserves original query: '{turn2_user['content']}'")

        # Check Turn 2 assistant message has execution_info with query_rewriting
        turn2_assistant = msgs[-1]
        assert turn2_assistant["role"] == "assistant"
        exec_info = turn2_assistant.get("execution_info") or {}
        assert exec_info.get("is_rewritten") is True
        assert exec_info.get("original_query") == q2
        assert exec_info.get("rewritten_query") is not None
        print(f"Verified ExecutionInfo in DB records rewrite metrics:")
        print(f"  - is_rewritten: {exec_info.get('is_rewritten')}")
        print(f"  - original_query: {exec_info.get('original_query')}")
        print(f"  - rewritten_query: {exec_info.get('rewritten_query')}")
        print(f"  - rewrite_duration_ms: {exec_info.get('rewrite_duration_ms')}ms")

    print("\n" + "=" * 65)
    print("M2.3 Live Query Rewriting Verification: ALL CHECKS PASSED!")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(main())
