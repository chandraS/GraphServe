"""Optional SIE embeddings; similarity ranking stays in the Python agent."""
import asyncio
import math
import os
import httpx


async def rerank(question, evidence):
    api_key = os.getenv("SIE_API_KEY")
    headers = {"Authorization": "Bearer " + api_key} if api_key else {}
    async with httpx.AsyncClient(timeout=90) as client:
        for attempt in range(5):
            response = await client.post(os.environ["SIE_BASE_URL"].rstrip("/") + "/v1/embeddings",
                headers=headers,
                json={"model": os.getenv("SIE_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
                      "input": [question] + [e["text"] for e in evidence]})
            if response.status_code != 503 or attempt == 4:
                break
            await asyncio.sleep(2 ** attempt)
        response.raise_for_status()
        rows = sorted(response.json()["data"], key=lambda r: r["index"])
    if [r["index"] for r in rows] != list(range(len(evidence) + 1)):
        raise ValueError("SIE returned incomplete embeddings")
    vectors = [r["embedding"] for r in rows]
    query = vectors[0]
    def similarity(v):
        if len(v) != len(query) or not all(math.isfinite(x) for x in v + query):
            raise ValueError("Invalid embedding")
        norm = math.sqrt(sum(x*x for x in query) * sum(x*x for x in v))
        return sum(a*b for a, b in zip(query, v)) / norm if norm else 0
    return [e for _, e in sorted(zip([similarity(v) for v in vectors[1:]], evidence),
                                 key=lambda pair: pair[0], reverse=True)]
