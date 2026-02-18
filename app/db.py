from __future__ import annotations

import io
import os

from dotenv import load_dotenv
from supabase import create_client, Client

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
# Projects
# ---------------------------------------------------------------------------

def create_project(name: str, contract_pdf_path: str | None = None) -> dict:
    sb = get_client()
    resp = sb.table("projects").insert({
        "name": name,
        "contract_pdf_path": contract_pdf_path,
    }).execute()
    return resp.data[0]


def list_projects() -> list[dict]:
    sb = get_client()
    resp = sb.table("projects").select("*").order("created_at", desc=True).execute()
    return resp.data


def get_project(project_id: str) -> dict | None:
    sb = get_client()
    resp = sb.table("projects").select("*").eq("id", project_id).maybe_single().execute()
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
    return resp.data


def upsert_product(data: dict) -> dict:
    sb = get_client()
    data["model_number"] = data["model_number"].strip().upper()
    resp = sb.table("products").upsert(data, on_conflict="model_number").execute()
    return resp.data[0]


# ---------------------------------------------------------------------------
# Project items (line items extracted from a contract)
# ---------------------------------------------------------------------------

def create_project_items(items: list[dict]) -> list[dict]:
    sb = get_client()
    for item in items:
        if "model_number" in item and item["model_number"]:
            item["model_number"] = item["model_number"].strip().upper()
    resp = sb.table("project_items").insert(items).execute()
    return resp.data


def get_project_items(project_id: str) -> list[dict]:
    sb = get_client()
    resp = (
        sb.table("project_items")
        .select("*, products(*)")
        .eq("project_id", project_id)
        .order("created_at")
        .execute()
    )
    return resp.data


def update_project_item(item_id: str, data: dict) -> dict:
    sb = get_client()
    resp = sb.table("project_items").update(data).eq("id", item_id).execute()
    return resp.data[0]


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
    return resp["signedURL"]
