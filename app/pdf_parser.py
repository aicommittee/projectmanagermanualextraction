from __future__ import annotations

import io
import json
import os

import pdfplumber
from anthropic import Anthropic

_SYSTEM_PROMPT = """You are a specialist in AV (audio-visual) and smart home systems for a company called ATI of America.
You are given the raw text of a project contract and must extract a structured list of the main installed
products that would have user manuals and manufacturer warranties.

INCLUDE:
- AV equipment (TVs, displays, projectors, screens)
- AV processors, receivers, amplifiers
- Speakers, subwoofers, soundbars
- Networking equipment (routers, switches, access points, racks)
- Control systems (Crestron, Savant, Control4, Lutron, etc.)
- Smart home devices (thermostats, keypads, dimmers, shades)
- Security cameras and systems
- Distribution equipment (HDMI matrices, video extenders)
- Streaming devices (Apple TV, etc.)

EXCLUDE:
- Labor, programming, installation, or service line items
- Small parts, cables, wire, connectors, fasteners, conduit
- Rack shelves, blank panels, mounting hardware
- Batteries, consumables
- ATI-branded items like "ATI Of America RACK WIRING PACKAGE"
- Notes, warranties sold separately, or non-product lines
- Items with "NOTE", "ALLOWANCE", or clearly administrative entries

Return ONLY a JSON array (no markdown fences, no extra text):
[
  {"brand": "Crestron", "model_number": "DM-NVX-D30", "product_name": "Crestron 4K60 Network AV Decoder"},
  {"brand": "Samsung", "model_number": "QN55Q80DAFXZA", "product_name": "Samsung 55\\" QLED Smart TV"}
]"""


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract text from all pages of a PDF."""
    pages: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
    return "\n".join(pages)


def parse_products_from_text(contract_text: str) -> list[dict]:
    """Send contract text to Claude Opus and return a deduplicated product list."""
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    response = client.messages.create(
        model="claude-opus-4-5-20251101",
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": contract_text}],
    )

    text = response.content[0].text.strip()

    # Strip markdown fences if present
    if text.startswith("```"):
        # Remove opening fence (with optional language tag)
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        else:
            text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    products = json.loads(text)

    # Deduplicate by model_number (case-insensitive), keep first occurrence
    seen: set[str] = set()
    unique: list[dict] = []
    for p in products:
        key = p["model_number"].strip().upper()
        if key not in seen:
            seen.add(key)
            p["model_number"] = key
            unique.append(p)

    return unique


def parse_products_from_pdf(pdf_bytes: bytes) -> list[dict]:
    """Extract text from a PDF, then parse products with Claude."""
    text = extract_text_from_pdf(pdf_bytes)
    return parse_products_from_text(text)
