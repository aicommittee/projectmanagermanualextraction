from __future__ import annotations

import io
import logging
import os

from dotenv import load_dotenv
from supabase import create_client, Client

logger = logging.getLogger(__name__)

_client: Client | None = None


def get_client() -> Client:
    """Return a singleton Supabase client."""
    global _client
    if _client is None:
        load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_KEY"]
        _client = create_client(url, key)
    return _client


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(email: str, password_hash: str, name: str, role: str = "pm", approved: bool = False) -> dict:
    sb = get_client()
    resp = sb.table("users").insert({
        "email": email.strip().lower(),
        "password_hash": password_hash,
        "name": name.strip(),
        "role": role,
        "approved": approved,
    }).execute()
    return resp.data[0]


def get_user_by_email(email: str) -> dict | None:
    sb = get_client()
    resp = sb.table("users").select("*").eq("email", email.strip().lower()).maybe_single().execute()
    if resp is None:
        return None
    return resp.data


def get_user_by_id(user_id: str) -> dict | None:
    sb = get_client()
    resp = sb.table("users").select("*").eq("id", user_id).maybe_single().execute()
    if resp is None:
        return None
    return resp.data


def list_users() -> list[dict]:
    sb = get_client()
    resp = sb.table("users").select("id, email, name, role, approved, created_at").order("created_at", desc=True).execute()
    return resp.data or []


def update_user(user_id: str, data: dict) -> dict:
    sb = get_client()
    resp = sb.table("users").update(data).eq("id", user_id).execute()
    if resp is None or not resp.data:
        return data
    return resp.data[0]


def delete_user(user_id: str):
    sb = get_client()
    sb.table("users").delete().eq("id", user_id).execute()


def count_users() -> int:
    sb = get_client()
    resp = sb.table("users").select("id", count="exact").execute()
    return resp.count if resp.count is not None else 0


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def create_project(name: str, user_id: str | None = None, contract_pdf_path: str | None = None) -> dict:
    sb = get_client()
    row = {"name": name, "contract_pdf_path": contract_pdf_path}
    if user_id:
        row["user_id"] = user_id
    resp = sb.table("projects").insert(row).execute()
    return resp.data[0]


def list_projects(user_id: str | None = None) -> list[dict]:
    sb = get_client()
    if user_id:
        resp = sb.table("projects").select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
    else:
        # Admin view: include user info
        resp = sb.table("projects").select("*, users(name, email)").order("created_at", desc=True).execute()
    return resp.data or []


def get_project(project_id: str) -> dict | None:
    sb = get_client()
    resp = sb.table("projects").select("*").eq("id", project_id).maybe_single().execute()
    if resp is None:
        return None
    return resp.data


def delete_project(project_id: str):
    sb = get_client()
    sb.table("projects").delete().eq("id", project_id).execute()


# ---------------------------------------------------------------------------
# Product library (cached across all projects)
# ---------------------------------------------------------------------------

def get_product_by_model(model_number: str) -> dict | None:
    sb = get_client()
    model_number = model_number.strip().upper()
    resp = sb.table("products").select("*").eq("model_number", model_number).maybe_single().execute()
    if resp is None:
        return None
    return resp.data


def upsert_product(data: dict) -> dict:
    sb = get_client()
    data["model_number"] = data["model_number"].strip().upper()
    resp = sb.table("products").upsert(data, on_conflict="model_number").execute()
    if resp is None or not resp.data:
        logger.error("upsert_product returned no data for %s", data.get("model_number"))
        return data
    return resp.data[0]


def list_products(search: str | None = None) -> list[dict]:
    sb = get_client()
    query = sb.table("products").select("*").order("created_at", desc=True)
    if search:
        query = query.or_(f"model_number.ilike.%{search}%,brand.ilike.%{search}%")
    resp = query.execute()
    return resp.data or []


def update_product(product_id: str, data: dict) -> dict:
    sb = get_client()
    if "model_number" in data and data["model_number"]:
        data["model_number"] = data["model_number"].strip().upper()
    resp = sb.table("products").update(data).eq("id", product_id).execute()
    if resp is None or not resp.data:
        logger.error("update_product returned no data for %s", product_id)
        return data
    return resp.data[0]


def delete_product(product_id: str):
    sb = get_client()
    sb.table("project_items").update({"product_id": None}).eq("product_id", product_id).execute()
    sb.table("products").delete().eq("id", product_id).execute()


# ---------------------------------------------------------------------------
# Project items (line items extracted from a contract)
# ---------------------------------------------------------------------------

def create_project_items(items: list[dict]) -> list[dict]:
    sb = get_client()
    for item in items:
        if "model_number" in item and item["model_number"]:
            item["model_number"] = item["model_number"].strip().upper()
    resp = sb.table("project_items").insert(items).execute()
    return resp.data or []


def get_project_items(project_id: str) -> list[dict]:
    sb = get_client()
    resp = (
        sb.table("project_items")
        .select("*, products(*)")
        .eq("project_id", project_id)
        .order("created_at")
        .execute()
    )
    return resp.data or []


def update_project_item(item_id: str, data: dict) -> dict:
    sb = get_client()
    resp = sb.table("project_items").update(data).eq("id", item_id).execute()
    if resp is None or not resp.data:
        logger.error("update_project_item returned no data for %s", item_id)
        return data
    return resp.data[0]


def delete_project_item(item_id: str):
    sb = get_client()
    sb.table("project_items").delete().eq("id", item_id).execute()


def delete_project_items(item_ids: list[str]):
    """Delete multiple project items by ID."""
    sb = get_client()
    sb.table("project_items").delete().in_("id", item_ids).execute()


# ---------------------------------------------------------------------------
# Supabase Storage (manuals bucket)
# ---------------------------------------------------------------------------

def upload_manual(pdf_bytes: bytes, storage_path: str) -> str:
    sb = get_client()
    sb.storage.from_("manuals").upload(
        path=storage_path,
        file=pdf_bytes,
        file_options={"content-type": "application/pdf", "upsert": "true"},
    )
    return storage_path


def get_manual_url(storage_path: str) -> str:
    sb = get_client()
    resp = sb.storage.from_("manuals").create_signed_url(storage_path, 365 * 24 * 60 * 60)
    # Handle both possible key names from different SDK versions
    if isinstance(resp, dict):
        return resp.get("signedURL") or resp.get("signedUrl") or ""
    return ""
