"""
Supabase client for storing tender snapshots.

This module handles:
- Connection to Supabase
- Inserting data into snapshots table (historical record)
- Upserting data into latest_snapshot table (latest data only)
"""

import os
from datetime import datetime
from typing import Dict, List, Optional

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()


class SupabaseClient:
    """Supabase client for tender data management."""

    def __init__(self):
        """Initialize Supabase client with credentials from environment."""
        self.supabase_url = os.getenv("SUPABASE_URL")
        self.supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")  # Use service role for server-side operations
        
        if not self.supabase_url or not self.supabase_key:
            raise ValueError(
                "Missing Supabase credentials. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env file"
            )
        
        self.client: Client = create_client(self.supabase_url, self.supabase_key)
    
    def save_snapshot(
        self,
        tenders: List[Dict],
        source: str,
        live_tenders: Optional[int] = None,
    ) -> Dict[str, any]:
        """
        Save tender snapshot to Supabase.
        
        Inserts into:
        1. snapshots table (historical record)
        2. latest_snapshot table (overwrites previous latest)
        
        Args:
            tenders: List of tender dictionaries
            source: Data source identifier (e.g., "eprocure.gov.in/AdvancedSearch")
            live_tenders: Total number of live tenders (optional)
        
        Returns:
            Dict with success status and details
        """
        try:
            scraped_at = datetime.utcnow().isoformat()
            count = len(tenders)
            
            # Prepare snapshot data
            snapshot_data = {
                "scraped_at": scraped_at,
                "live_tenders": live_tenders,
                "count": count,
                "payload": tenders,
                "source": source,
            }
            
            # 1. Insert into snapshots table (historical record)
            snapshots_result = self.client.table("snapshots").insert(snapshot_data).execute()
            
            # 2. Upsert into latest_snapshot table (always id=1)
            latest_snapshot_data = {
                "id": 1,  # Always use id=1 to ensure single row
                "scraped_at": scraped_at,
                "live_tenders": live_tenders,
                "count": count,
                "payload": tenders,
                "source": source,
                "updated_at": scraped_at,
            }
            
            latest_result = self.client.table("latest_snapshot").upsert(
                latest_snapshot_data,
                on_conflict="id"
            ).execute()
            
            return {
                "success": True,
                "message": "Snapshot saved successfully",
                "snapshot_id": snapshots_result.data[0]["id"] if snapshots_result.data else None,
                "count": count,
                "scraped_at": scraped_at,
            }
        
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "message": "Failed to save snapshot to Supabase",
            }


# Singleton instance
_supabase_client: Optional[SupabaseClient] = None


def get_supabase_client() -> Optional[SupabaseClient]:
    """
    Get or create Supabase client singleton.
    
    Returns:
        SupabaseClient instance or None if credentials not configured
    """
    global _supabase_client
    
    if _supabase_client is None:
        try:
            _supabase_client = SupabaseClient()
        except ValueError as e:
            print(f"⚠️  Supabase not configured: {e}")
            return None
    
    return _supabase_client


def save_to_supabase(
    tenders: List[Dict],
    source: str,
    live_tenders: Optional[int] = None,
) -> Dict[str, any]:
    """
    Save tender snapshot to Supabase (convenience function).
    
    Args:
        tenders: List of tender dictionaries
        source: Data source identifier
        live_tenders: Total number of live tenders (optional)
    
    Returns:
        Dict with success status and details
    """
    client = get_supabase_client()
    
    if client is None:
        return {
            "success": False,
            "error": "Supabase client not configured",
            "message": "Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY environment variables",
        }
    
    return client.save_snapshot(tenders, source, live_tenders)

