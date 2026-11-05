import asyncio
from app.db.session import async_session_factory
from tests.evaluate_m3_3_reranker import setup_benchmark_kb

async def main():
    async with async_session_factory() as session:
        kb = await setup_benchmark_kb(session)
        print("Benchmark KB ready:", kb.id, kb.name)

if __name__ == "__main__":
    asyncio.run(main())
