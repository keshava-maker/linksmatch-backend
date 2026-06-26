from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import httpx
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

SERPAPI_KEY = os.getenv("SERPAPI_KEY")

STORES = {
    "nykaa": "Nykaa",
    "purplle": "Purplle",
    "amazon": "Amazon",
    "flipkart": "Flipkart",
    "myntra": "Myntra",
}

app = FastAPI(title="LinkMatch API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
async def health():
    return {"status": "ok"}

async def search_store(store_key: str, search_query: str, brand: str, size: str):
    """Search a single store via SerpApi"""
    try:
        params = {
            "api_key": SERPAPI_KEY,
            "engine": "google",
            "q": search_query,
            "gl": "in",
            "tbm": "shop",
        }

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get("https://api.serpapi.com/search", params=params)
            data = response.json()

        shopping_results = data.get("shopping_results", [])
        if not shopping_results:
            return None

        best_match = None
        best_confidence = 0

        for result in shopping_results[:5]:
            title = (result.get("title") or "").lower()
            price_str = result.get("price", "")
            link = result.get("link", "")

            if not link:
                continue

            conf = 0.0

            if brand.lower() in title:
                conf += 0.4
            else:
                continue

            if size:
                if size.lower() in title:
                    conf += 0.6
            else:
                conf += 0.3

            if conf > best_confidence:
                best_confidence = conf
                best_match = {
                    "store": STORES[store_key],
                    "title": result.get("title", ""),
                    "price": price_str,
                    "link": link,
                    "confidence": round(conf, 2),
                }

        if best_match and best_confidence >= 0.4:
            return best_match
        return None

    except Exception as e:
        print(f"Search error on {store_key}: {e}")
        return None
class CompareRequest(BaseModel):
    brand: str
    size: Optional[str] = None
    search_query: str
@app.post("/api/compare")
async def compare_prices(req: CompareRequest):
    """Main endpoint: search for a product across all stores"""
    
    if not SERPAPI_KEY:
        return {"error": "SERPAPI_KEY not configured", "matches": []}
    
    print(f"Searching for {req.brand} {req.size} | Query: {req.search_query}")
    
    tasks = [
        search_store(store_key, req.search_query, req.brand, req.size)
        for store_key in STORES.keys()
    ]
    
    results = await asyncio.gather(*tasks)
    results = [r for r in results if r is not None]
    
    return {
        "matches": results,
        "message": f"Found on {len(results)} store(s)"
    }