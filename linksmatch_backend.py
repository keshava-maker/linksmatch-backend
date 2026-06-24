from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import os
import json
from datetime import datetime, timedelta
import asyncio
from typing import Optional, List
import psycopg2
from psycopg2.extras import RealDictCursor
import re

# ── DATABASE SETUP ────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/linksmatch")

def init_db():
    """Create tables if they don't exist"""
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS product_cache (
            id SERIAL PRIMARY KEY,
            search_hash VARCHAR(255) UNIQUE NOT NULL,
            brand VARCHAR(100),
            size VARCHAR(50),
            search_query TEXT,
            results JSONB,
            created_at TIMESTAMP DEFAULT NOW(),
            expires_at TIMESTAMP
        )
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS store_matches (
            id SERIAL PRIMARY KEY,
            search_hash VARCHAR(255),
            store_name VARCHAR(50),
            product_name TEXT,
            product_url TEXT,
            price DECIMAL(10,2),
            in_stock BOOLEAN,
            confidence DECIMAL(3,2),
            created_at TIMESTAMP DEFAULT NOW(),
            FOREIGN KEY (search_hash) REFERENCES product_cache(search_hash)
        )
    """)
    
    conn.commit()
    cur.close()
    conn.close()

def get_db():
    return psycopg2.connect(DATABASE_URL)

def get_cached_results(search_hash):
    """Get cached results if they exist and haven't expired"""
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute("""
        SELECT results FROM product_cache 
        WHERE search_hash = %s AND expires_at > NOW()
    """, (search_hash,))
    
    result = cur.fetchone()
    cur.close()
    conn.close()
    
    return json.loads(result['results']) if result else None

def cache_results(search_hash, brand, size, search_query, results):
    """Cache search results for 4 hours"""
    conn = get_db()
    cur = conn.cursor()
    
    expires_at = datetime.now() + timedelta(hours=4)
    
    try:
        cur.execute("""
            INSERT INTO product_cache (search_hash, brand, size, search_query, results, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (search_hash) DO UPDATE SET
                results = EXCLUDED.results,
                expires_at = EXCLUDED.expires_at
        """, (search_hash, brand, size, search_query, json.dumps(results), expires_at))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Cache error: {e}")
    finally:
        cur.close()
        conn.close()

# ── SERAPI STORE CONFIGS ──────────────────────────────────────────────────
STORES = {
    "Nykaa": {
        "domain": "nykaa.com",
        "engine": "google",
        "tbs": "qdr:m",  # Past month
    },
    "Purplle": {
        "domain": "purplle.com",
        "engine": "google",
        "tbs": "qdr:m",
    },
    "Amazon": {
        "domain": "amazon.in",
        "engine": "google",
        "tbs": "qdr:m",
    },
    "Flipkart": {
        "domain": "flipkart.com",
        "engine": "google",
        "tbs": "qdr:m",
    },
    "Myntra": {
        "domain": "myntra.com",
        "engine": "google",
        "tbs": "qdr:m",
    },
}

# ── HELPER FUNCTIONS ──────────────────────────────────────────────────────
def extract_price(text):
    """Extract price from text"""
    if not text:
        return None
    match = re.search(r'₹\s*([\d,]+(?:\.\d{2})?)', text)
    if match:
        return float(match.group(1).replace(',', ''))
    return None

def normalize_text(text):
    """Normalize text for matching"""
    return re.sub(r'[^a-z0-9]', '', text.lower())

def calculate_confidence(result_title, brand, size, search_query):
    """
    Calculate confidence that this result matches the original product
    Returns 0-1 score
    """
    title_norm = normalize_text(result_title)
    brand_norm = normalize_text(brand)
    
    confidence = 0.0
    
    # Brand match (heavy weight)
    if brand_norm in title_norm:
        confidence += 0.6
    
    # Size match (if provided)
    if size:
        size_pattern = normalize_text(size)
        if size_pattern in title_norm:
            confidence += 0.3
    else:
        confidence += 0.15  # Small boost if we're not even checking size
    
    # Query word matches
    query_words = search_query.lower().split()
    matched_words = sum(1 for word in query_words if len(word) > 3 and word in title_norm)
    if query_words:
        confidence += min(0.1, (matched_words / len(query_words)) * 0.1)
    
    return min(1.0, confidence)

async def search_store(store_name, search_query, brand, size, serp_api_key):
    """Search a single store via SerpApi"""
    store_config = STORES[store_name]
    
    search_params = {
        "q": f"{search_query} site:{store_config['domain']}",
        "engine": store_config['engine'],
        "api_key": serp_api_key,
        "num": 10,
        "tbm": "shop",  # Shopping results
    }
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get("https://serpapi.com/search", params=search_params)
            data = response.json()
            
            results = []
            if "shopping_results" in data:
                for item in data["shopping_results"][:3]:  # Top 3 results
                    title = item.get("title", "")
                    confidence = calculate_confidence(title, brand, size, search_query)
                    
                    # Only include if confidence is high enough
                    if confidence > 0.45:
                        result = {
                            "store": store_name,
                            "product_name": title,
                            "product_url": item.get("link", ""),
                            "price": extract_price(item.get("price", "")),
                            "in_stock": item.get("rating") is not None,  # Heuristic
                            "confidence": float(confidence),
                            "source": "shopping_results"
                        }
                        results.append(result)
            
            # Also try organic results as fallback
            if not results and "organic_results" in data:
                for item in data["organic_results"][:2]:
                    title = item.get("title", "")
                    confidence = calculate_confidence(title, brand, size, search_query)
                    
                    if confidence > 0.5:
                        result = {
                            "store": store_name,
                            "product_name": title,
                            "product_url": item.get("link", ""),
                            "price": None,  # No price in organic results typically
                            "in_stock": True,
                            "confidence": float(confidence),
                            "source": "organic_results"
                        }
                        results.append(result)
            
            return results
    
    except Exception as e:
        print(f"Error searching {store_name}: {e}")
        return []

async def search_all_stores(search_query, brand, size, serp_api_key):
    """Search all stores in parallel"""
    tasks = [
        search_store(store_name, search_query, brand, size, serp_api_key)
        for store_name in STORES.keys()
    ]
    
    results_per_store = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_results = []
    for store_results in results_per_store:
        if isinstance(store_results, list):
            all_results.extend(store_results)
        elif isinstance(store_results, Exception):
            print(f"Task error: {store_results}")
    
    return all_results

# ── FASTAPI APP ───────────────────────────────────────────────────────────
app = FastAPI()

# CORS for frontend access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Update this to your frontend domain in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class CompareRequest(BaseModel):
    brand: str
    size: Optional[str] = None
    search_query: str

@app.on_event("startup")
async def startup_event():
    """Initialize database on startup"""
    init_db()

@app.post("/api/compare")
async def compare_prices(req: CompareRequest):
    """
    Main endpoint: search all stores and return matches
    """
    serp_api_key = os.getenv("SERPAPI_KEY")
    if not serp_api_key:
        raise HTTPException(status_code=500, detail="SerpApi key not configured")
    
    # Create search hash for caching
    search_hash = f"{req.brand}_{req.size or 'nosize'}_{req.search_query}".replace(" ", "_")
    
    # Check cache first
    cached = get_cached_results(search_hash)
    if cached:
        return {
            "from_cache": True,
            "matches": cached,
            "store_availability": {m["store"]: m for m in cached}
        }
    
    # Search all stores
    results = await search_all_stores(req.search_query, req.brand, req.size, serp_api_key)
    
    # Group by store and pick best match per store
    store_matches = {}
    for result in results:
        store = result["store"]
        if store not in store_matches or result["confidence"] > store_matches[store]["confidence"]:
            store_matches[store] = result
    
    # Convert to list and sort by confidence
    matches = list(store_matches.values())
    matches.sort(key=lambda x: x["confidence"], reverse=True)
    
    # Cache results
    cache_results(search_hash, req.brand, req.size, req.search_query, matches)
    
    return {
        "from_cache": False,
        "matches": matches,
        "store_availability": store_matches
    }

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
