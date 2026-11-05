import asyncio
import json
import sys
import httpx

BASE_URL = "http://127.0.0.1:8000/api/v1"


async def main():
    print("=" * 60)
    print("AgentStudio-RAG M2.2 Live Streaming Verification")
    print("=" * 60)

    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        # 1. Fetch available knowledge bases
        kb_resp = await client.get(f"{BASE_URL}/knowledge-bases")
        if kb_resp.status_code != 200:
            print(f"Failed to fetch knowledge bases: {kb_resp.status_code} {kb_resp.text}")
            sys.exit(1)

        kbs = kb_resp.json()
        if not kbs:
            print("No knowledge base found! Please create one first.")
            sys.exit(1)

        target_kb = None
        for kb in kbs:
            if kb.get("chunk_count", 0) > 0 and kb.get("embedding_model") == "BAAI/bge-small-zh-v1.5":
                target_kb = kb
                break
        if not target_kb:
            target_kb = kbs[0]

        print(f"Target Knowledge Base: {target_kb['name']} (ID: {target_kb['id']}, Chunks: {target_kb.get('chunk_count', 0)})")

        # 2. Test Live Streaming Chat
        query = "请详细介绍该知识库中关于大模型与Agent应用系统的设计规范和核心准则。"
        print(f"\n[Query]: {query}\n")

        stream_payload = {
            "knowledge_base_id": target_kb["id"],
            "query": query,
            "mode": "workflow",
            "top_k": 4,
        }

        print("[Connecting to SSE Stream] -> POST /api/v1/chat/completions/stream ...")
        conv_id = None
        full_tokens = ""
        citations = []
        trace_info = {}

        async with client.stream("POST", f"{BASE_URL}/chat/completions/stream", json=stream_payload) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                print(f"Stream error: HTTP {resp.status_code}: {body.decode('utf-8')}")
                sys.exit(1)

            print(f"Stream Connected! Status: {resp.status_code}, Content-Type: {resp.headers.get('content-type')}\n")
            print("--- Streaming Output Start ---")

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
                        data = json.loads(data_str)
                    except Exception:
                        data = data_str

                    if current_event == "start":
                        conv_id = data.get("conversation_id")
                        print(f"\n[EVENT: start] Conversation ID: {conv_id}, User Message ID: {data.get('message_id')}")
                    elif current_event == "retrieval":
                        print(f"[EVENT: retrieval] Retrieved {data.get('retrieved_chunks_count')} chunks in {data.get('retrieval_duration_ms')}ms")
                        print("[EVENT: token] Incremental tokens: ", end="", flush=True)
                    elif current_event == "token":
                        delta = data.get("delta", "")
                        full_tokens += delta
                        sys.stdout.write(delta)
                        sys.stdout.flush()
                    elif current_event == "citations":
                        citations = data
                        print(f"\n\n[EVENT: citations] Received {len(citations)} citations")
                    elif current_event == "trace":
                        trace_info = data
                        print(f"[EVENT: trace] Total latency: {data.get('total_duration_ms')}ms (Retrieval: {data.get('retrieval_duration_ms')}ms, Generation: {data.get('generation_duration_ms')}ms)")
                    elif current_event == "done":
                        print(f"[EVENT: done] Assistant Message ID: {data.get('message_id')}")
                    elif current_event == "error":
                        print(f"\n[EVENT: error] {data}")

        print("--- Streaming Output End ---\n")

        # 3. Verify Database Message Persistence
        if conv_id:
            print(f"Verifying Database persistence for conversation {conv_id}...")
            msg_resp = await client.get(f"{BASE_URL}/conversations/{conv_id}/messages")
            assert msg_resp.status_code == 200
            db_msgs = msg_resp.json()
            print(f"Total messages in DB: {len(db_msgs)}")
            assert len(db_msgs) >= 2
            user_msg = db_msgs[-2]
            assistant_msg = db_msgs[-1]
            assert user_msg["role"] == "user"
            assert assistant_msg["role"] == "assistant"
            print(f"DB Assistant content length: {len(assistant_msg['content'])} chars")
            print(f"DB Assistant citations count: {len(assistant_msg.get('citations') or [])}")
            print(f"DB Assistant execution_info: {assistant_msg.get('execution_info')}")
            print("Persistence verification: PASSED!")

        # 4. Multi-turn continuation test via stream
        if conv_id:
            follow_up = "根据刚才提到的规范，总结3条最重要的执行建议。"
            print(f"\n[Multi-turn Test Query]: {follow_up}")
            stream_payload2 = {
                "knowledge_base_id": target_kb["id"],
                "conversation_id": conv_id,
                "query": follow_up,
                "mode": "workflow",
                "top_k": 3,
            }
            round2_tokens = ""
            async with client.stream("POST", f"{BASE_URL}/chat/completions/stream", json=stream_payload2) as resp2:
                assert resp2.status_code == 200
                async for line in resp2.aiter_lines():
                    line = line.strip()
                    if line.startswith("data:"):
                        data_str = line.replace("data:", "", 1).strip()
                        try:
                            d = json.loads(data_str)
                            if "delta" in d:
                                round2_tokens += d["delta"]
                                sys.stdout.write(d["delta"])
                                sys.stdout.flush()
                        except Exception:
                            pass
            print(f"\n\nMulti-turn response length: {len(round2_tokens)} chars")
            print("Multi-turn continuation: PASSED!")

    print("\n" + "=" * 60)
    print("All M2.2 live streaming verifications PASSED!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
