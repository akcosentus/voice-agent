"""Lazy singleton Supabase client (sync API)."""

import os
from typing import Optional

from supabase import Client, create_client

_client: Optional[Client] = None


def get_supabase() -> Client:
    """Return shared Supabase client using service role key."""
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_KEY"]
        _client = create_client(url, key)
    return _client
