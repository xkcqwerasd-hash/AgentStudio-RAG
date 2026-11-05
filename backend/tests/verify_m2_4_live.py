import asyncio
import json
import httpx


async def verify_live_stream():
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        # 1. Get knowledge bases
        r = await client.get("http://127.0.0.1:8000/api/v1/knowledge-bases")
        assert r.status_code == 200
        kbs = r.json()
        print(f"Found {len(kbs)} knowledge bases.")
        if not kbs:
            print("No knowledge base found, skipping live stream.")
            return

        # Find a KB with matching BAAI/bge-small-zh-v1.5
        target_kb = None
        for kb in kbs:
            if kb.get("embedding_model") == "BAAI/bge-small-zh-v1.5":
                target_kb = kb
                break
        if not target_kb:
            target_kb = kbs[0]

        kb_id = target_kb["id"]
        kb_name = target_kb["name"]
        print(f"Testing with KB: {kb_name} ({kb_id})")

        # 2. Test Stream endpoint
        payload = {
            "knowledge_base_id": kb_id,
            "query": "请问网关连接池配置参数是什么？",
            "mode": "workflow",
        }
        print("Calling /api/v1/chat/completions/stream...")
        events = []
        async with client.stream("POST", "http://127.0.0.1:8000/api/v1/chat/completions/stream", json=payload) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if line.startswith("event:"):
                    evt = line.replace("event:", "").strip()
                elif line.startswith("data:"):
                    d = line.replace("data:", "").strip()
                    events.append((evt, d))

        print(f"Received {len(events)} events:")
        event_types = set()
        for evt, d in events:
            event_types.add(evt)
            if evt in ("query_rewrite", "retrieval", "context_budget", "trace"):
                print(f"  [{evt}]: {d[:120]}...")

        assert "start" in event_types
        assert "context_budget" in event_types
        assert "token" in event_types
        assert "trace" in event_types
        assert "done" in event_types
        print("\nAll expected SSE events verified live successfully on port 8000!")


if __name__ == "__main__":
    asyncio.run(verify_live_stream())
