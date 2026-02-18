from __future__ import annotations

import io
import os
import threading
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import db
from .manual_finder import find_manual_and_warranty
from .pdf_parser import parse_products_from_pdf

# Load environment variables early
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

app = FastAPI(title="ATI Manual Finder")

# ---------------------------------------------------------------------------
# In-memory progress tracker
# ---------------------------------------------------------------------------
_progress: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _process_project(project_id: str, total: int):
    """Process all pending items for a project (runs in background thread)."""
    try:
        items = db.get_project_items(project_id)
        pending = [i for i in items if i["status"] == "pending"]

        for idx, item in enumerate(pending, 1):
            _progress[project_id] = {
                "message": f"Searching {idx}/{total}: {item.get('brand', '')} {item['model_number']}",
                "done": False,
                "error": None,
            }

            model = item["model_number"]

            # Check product library cache first
            cached = db.get_product_by_model(model)
            if cached and cached.get("manual_source_url"):
                manual_url = None
                if cached.get("manual_storage_path"):
                    try:
                        manual_url = db.get_manual_url(cached["manual_storage_path"])
                    except Exception:
                        manual_url = cached.get("manual_source_url")

                db.update_project_item(item["id"], {
                    "product_id": cached["id"],
                    "status": "found",
                    "manual_url": manual_url or cached.get("manual_source_url"),
                })
                continue

            # Web search
            result = find_manual_and_warranty(
                item.get("brand", ""),
                model,
                item.get("product_name", ""),
            )

            storage_path = None
            manual_url = None

            if result["manual_pdf_bytes"]:
                safe_brand = (item.get("brand") or "unknown").replace(" ", "_")
                storage_path = f"{project_id}/{safe_brand}_{model}_manual.pdf"
                try:
                    db.upload_manual(result["manual_pdf_bytes"], storage_path)
                    manual_url = db.get_manual_url(storage_path)
                except Exception:
                    manual_url = result.get("manual_source_url")

            # Upsert product library
            product = db.upsert_product({
                "brand": item.get("brand"),
                "model_number": model,
                "product_name": item.get("product_name"),
                "manual_source_url": result.get("manual_source_url"),
                "manual_storage_path": storage_path,
                "warranty_length": result.get("warranty_length"),
                "last_verified": datetime.now(timezone.utc).isoformat(),
            })

            # Update project item
            db.update_project_item(item["id"], {
                "product_id": product["id"],
                "status": result["status"],
                "manual_url": manual_url or result.get("manual_source_url"),
            })

        _progress[project_id] = {"message": "Complete!", "done": True, "error": None}

    except Exception as e:
        _progress[project_id] = {"message": str(e), "done": True, "error": str(e)}


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/projects")
async def list_projects():
    return db.list_projects()


@app.post("/api/projects/upload")
async def upload_project(
    background_tasks: BackgroundTasks,
    project_name: str = Form(...),
    file: UploadFile = File(...),
):
    pdf_bytes = await file.read()

    # Parse products from PDF
    products = parse_products_from_pdf(pdf_bytes)

    # Create project
    project = db.create_project(name=project_name)
    project_id = project["id"]

    # Create project items
    items = [
        {
            "project_id": project_id,
            "brand": p.get("brand"),
            "model_number": p["model_number"],
            "product_name": p.get("product_name"),
            "raw_line_item": f"{p.get('brand', '')} {p['model_number']}",
            "status": "pending",
        }
        for p in products
    ]
    if items:
        db.create_project_items(items)

    # Initialize progress
    _progress[project_id] = {"message": "Starting...", "done": False, "error": None}

    # Kick off background processing
    background_tasks.add_task(_process_project, project_id, len(products))

    return {
        "project_id": project_id,
        "product_count": len(products),
        "products": products,
    }


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str):
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    items = db.get_project_items(project_id)
    progress = _progress.get(project_id, {"message": "Not started", "done": True, "error": None})
    return {"project": project, "items": items, "progress": progress}


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    db.delete_project(project_id)
    _progress.pop(project_id, None)
    return {"status": "deleted"}


@app.get("/api/projects/{project_id}/progress")
async def get_progress(project_id: str):
    return _progress.get(
        project_id, {"message": "Not started", "done": True, "error": None}
    )


@app.patch("/api/items/{item_id}")
async def update_item(item_id: str, data: dict):
    # If manual_url is provided, set status to manual_entry
    if "manual_url" in data and data["manual_url"]:
        data.setdefault("status", "manual_entry")
    return db.update_project_item(item_id, data)


@app.post("/api/items/{item_id}/retry")
async def retry_item(item_id: str):
    # Fetch the item to get brand/model/product_name
    sb = db.get_client()
    resp = sb.table("project_items").select("*").eq("id", item_id).maybe_single().execute()
    item = resp.data
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    result = find_manual_and_warranty(
        item.get("brand", ""),
        item["model_number"],
        item.get("product_name", ""),
    )

    storage_path = None
    manual_url = None

    if result["manual_pdf_bytes"]:
        safe_brand = (item.get("brand") or "unknown").replace(" ", "_")
        storage_path = f"{item['project_id']}/{safe_brand}_{item['model_number']}_manual.pdf"
        try:
            db.upload_manual(result["manual_pdf_bytes"], storage_path)
            manual_url = db.get_manual_url(storage_path)
        except Exception:
            manual_url = result.get("manual_source_url")

    product = db.upsert_product({
        "brand": item.get("brand"),
        "model_number": item["model_number"],
        "product_name": item.get("product_name"),
        "manual_source_url": result.get("manual_source_url"),
        "manual_storage_path": storage_path,
        "warranty_length": result.get("warranty_length"),
        "last_verified": datetime.now(timezone.utc).isoformat(),
    })

    updated = db.update_project_item(item_id, {
        "product_id": product["id"],
        "status": result["status"],
        "manual_url": manual_url or result.get("manual_source_url"),
    })

    return updated


# ---------------------------------------------------------------------------
# Excel export
# ---------------------------------------------------------------------------

@app.get("/api/projects/{project_id}/export")
async def export_excel(project_id: str):
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    items = db.get_project_items(project_id)

    wb = Workbook()

    # --- Sheet 1: Products & Manuals ---
    ws1 = wb.active
    ws1.title = "Products & Manuals"

    navy_fill = PatternFill(start_color="1A3A5C", end_color="1A3A5C", fill_type="solid")
    white_bold = Font(bold=True, color="FFFFFF")

    headers = ["Brand", "Model Number", "Product Name", "Warranty", "Manual Link", "Status", "Notes"]
    widths = [15, 22, 45, 20, 20, 15, 30]

    for col_idx, (header, width) in enumerate(zip(headers, widths), 1):
        cell = ws1.cell(row=1, column=col_idx, value=header)
        cell.fill = navy_fill
        cell.font = white_bold
        ws1.column_dimensions[get_column_letter(col_idx)].width = width

    status_colors = {
        "found": "C6EFCE",
        "not_found": "FFC7CE",
        "manual_entry": "FFEB9C",
        "pending": "D9D9D9",
    }

    for row_idx, item in enumerate(items, 2):
        ws1.cell(row=row_idx, column=1, value=item.get("brand"))
        ws1.cell(row=row_idx, column=2, value=item.get("model_number"))
        ws1.cell(row=row_idx, column=3, value=item.get("product_name"))

        # Warranty from linked product
        product = item.get("products")
        warranty = product.get("warranty_length") if product else None
        ws1.cell(row=row_idx, column=4, value=warranty)

        # Manual link as hyperlink
        manual_url = item.get("manual_url")
        if manual_url:
            cell = ws1.cell(row=row_idx, column=5, value="Open Manual")
            cell.hyperlink = manual_url
            cell.font = Font(color="0563C1", underline="single")
        else:
            ws1.cell(row=row_idx, column=5, value="")

        # Status with color
        status = item.get("status", "pending")
        status_cell = ws1.cell(row=row_idx, column=6, value=status)
        if status in status_colors:
            status_cell.fill = PatternFill(
                start_color=status_colors[status],
                end_color=status_colors[status],
                fill_type="solid",
            )

        ws1.cell(row=row_idx, column=7, value=item.get("notes"))

    # --- Sheet 2: Needs Manual Lookup ---
    ws2 = wb.create_sheet("Needs Manual Lookup")
    not_found = [i for i in items if i.get("status") == "not_found"]

    headers2 = [
        "Brand", "Model Number", "Product Name",
        "Manual URL (paste here)", "Warranty (enter here)", "Notes",
    ]
    for col_idx, header in enumerate(headers2, 1):
        cell = ws2.cell(row=1, column=col_idx, value=header)
        cell.fill = navy_fill
        cell.font = white_bold

    for row_idx, item in enumerate(not_found, 2):
        ws2.cell(row=row_idx, column=1, value=item.get("brand"))
        ws2.cell(row=row_idx, column=2, value=item.get("model_number"))
        ws2.cell(row=row_idx, column=3, value=item.get("product_name"))
        # Columns 4, 5, 6 left blank for user to fill in

    # Save to buffer
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    safe_name = project["name"].replace(" ", "_").replace("/", "_")
    filename = f"{safe_name}_manuals.xlsx"

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Serve frontend (must be LAST — catches all unmatched routes)
# ---------------------------------------------------------------------------

_frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(_frontend_dir):
    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
