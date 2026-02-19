"""
embeddings.py — OpenAI embedding wrapper.

Rules:
- Called ONCE per new tender (in cron, after scrape)
- Called ONCE per user preference save (in onboarding endpoint)
- NEVER called on dashboard load
- NEVER called per request

Model: text-embedding-3-small (1536 dimensions, cheapest, fast)
Cost:  ~$0.02 per 1M tokens — 2248 tenders ≈ $0.001 total
"""

import os
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_URL = "https://api.openai.com/v1/embeddings"


# ── text builders ─────────────────────────────────────────────────────────────

def build_tender_text(tender: dict) -> str:
    """
    Build a single string from tender fields for embedding.

    We combine the most semantically meaningful fields.
    More detail = better matching. Empty fields are skipped cleanly.

    Args:
        tender: A tenders table row as a dict

    Returns:
        A plain text string ready to be embedded
    """
    parts = []

    if tender.get("title"):
        parts.append(tender["title"])

    if tender.get("organisation"):
        parts.append(f"Organisation: {tender['organisation']}")

    if tender.get("product_category"):
        parts.append(f"Category: {tender['product_category']}")

    if tender.get("work_description"):
        parts.append(f"Description: {tender['work_description']}")

    if tender.get("location"):
        parts.append(f"Location: {tender['location']}")

    if tender.get("tender_type"):
        parts.append(f"Type: {tender['tender_type']}")

    if tender.get("tender_category"):
        parts.append(f"Tender Category: {tender['tender_category']}")

    # Fallback: if we only have title, that's still useful
    text = ". ".join(parts)

    if not text.strip():
        raise ValueError(f"Tender {tender.get('id')} has no embeddable text")

    return text


def build_user_preference_text(preferences: dict) -> str:
    """
    Build a single string from user_preferences fields for embedding.

    This is what gets compared against tender embeddings at match time.
    The richer this text, the better the matches.

    Args:
        preferences: A user_preferences table row as a dict,
                     optionally merged with profiles fields

    Returns:
        A plain text string ready to be embedded
    """
    parts = []

    if preferences.get("user_goal"):
        parts.append(f"Goal: {preferences['user_goal']}")

    if preferences.get("keywords"):
        kws = preferences["keywords"]
        if isinstance(kws, list) and kws:
            parts.append(f"Keywords: {', '.join(kws)}")

    if preferences.get("categories"):
        cats = preferences["categories"]
        if isinstance(cats, list) and cats:
            parts.append(f"Categories: {', '.join(cats)}")

    if preferences.get("regions"):
        regions = preferences["regions"]
        if isinstance(regions, list) and regions:
            parts.append(f"Regions: {', '.join(regions)}")

    if preferences.get("roles"):
        roles = preferences["roles"]
        if isinstance(roles, list) and roles:
            parts.append(f"Roles: {', '.join(roles)}")

    # From profiles table (pass these in if you have them)
    if preferences.get("primary_industry"):
        parts.append(f"Industry: {preferences['primary_industry']}")

    if preferences.get("secondary_industries"):
        si = preferences["secondary_industries"]
        if isinstance(si, list) and si:
            parts.append(f"Also works in: {', '.join(si)}")

    if preferences.get("business_type"):
        parts.append(f"Business type: {preferences['business_type']}")

    if not parts:
        raise ValueError("User preferences have no embeddable content")

    return ". ".join(parts)


# ── core embedding call ───────────────────────────────────────────────────────

async def get_embedding(text: str) -> list[float]:
    """
    Call OpenAI and return a 1536-dimension embedding vector.

    Args:
        text: Plain text to embed (max ~8000 tokens, ~6000 words)

    Returns:
        List of 1536 floats

    Raises:
        ValueError: if API key missing or response malformed
        httpx.HTTPError: on network/API failure
    """
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY not set in .env")

    # Truncate to safe length — OpenAI limit is 8191 tokens
    # ~4 chars per token, so 30000 chars is safely under limit
    text = text[:30000]

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            EMBEDDING_URL,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": EMBEDDING_MODEL,
                "input": text,
            },
        )
        response.raise_for_status()
        data = response.json()

    try:
        return data["data"][0]["embedding"]
    except (KeyError, IndexError) as e:
        raise ValueError(f"Unexpected OpenAI response: {data}") from e


# ── convenience wrappers ──────────────────────────────────────────────────────

async def embed_tender(tender: dict) -> Optional[list[float]]:
    """
    Build text from a tender row and return its embedding.

    Returns None on failure so the cron job can skip and continue.
    """
    try:
        text = build_tender_text(tender)
        embedding = await get_embedding(text)
        print(f"  ✅ Embedded tender: {tender.get('ref_no', 'unknown')[:40]}")
        return embedding
    except Exception as e:
        print(f"  ⚠️  Failed to embed tender {tender.get('id')}: {e}")
        return None


async def embed_user_preferences(preferences: dict) -> Optional[list[float]]:
    """
    Build text from user preferences and return its embedding.

    Returns None on failure.
    """
    try:
        text = build_user_preference_text(preferences)
        embedding = await get_embedding(text)
        print(f"  ✅ Embedded preferences for user: {preferences.get('user_id', 'unknown')}")
        return embedding
    except Exception as e:
        print(f"  ⚠️  Failed to embed user preferences: {e}")
        return None
