from __future__ import annotations

import os
import time
from urllib.parse import urlparse, parse_qs, unquote, urljoin

import requests
from anthropic import Anthropic
from bs4 import BeautifulSoup

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _clean_ddg_url(href: str) -> str | None:
    """Extract the real URL from a DuckDuckGo redirect wrapper."""
    if not href:
        return None
    if "uddg=" in href:
        parsed = urlparse(href)
        params = parse_qs(parsed.query)
        if "uddg" in params:
            return unquote(params["uddg"][0])
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return None


def _search_duckduckgo(query: str) -> list[str]:
    """Search DuckDuckGo HTML and return up to 10 result URLs."""
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    urls: list[str] = []

    for tag in soup.select("a.result__url"):
        url = _clean_ddg_url(tag.get("href", ""))
        if url:
            urls.append(url)

    for tag in soup.select("a.result__a"):
        url = _clean_ddg_url(tag.get("href", ""))
        if url:
            urls.append(url)

    # Deduplicate while preserving order, skip DuckDuckGo URLs
    seen: set[str] = set()
    unique: list[str] = []
    for u in urls:
        if u not in seen and "duckduckgo.com" not in u:
            seen.add(u)
            unique.append(u)

    return unique[:10]


def _fetch_page_text(url: str) -> str:
    """Fetch a page and return up to 8000 chars of cleaned text."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception:
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return text[:8000]


def _try_download_pdf(url: str) -> bytes | None:
    """Try to download a PDF from a URL. Returns bytes if successful."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=30, stream=True)
        content_type = resp.headers.get("Content-Type", "")
        if "pdf" in content_type.lower():
            return resp.content
        # Also check for PDF magic bytes as fallback
        content = resp.content
        if content[:5] == b"%PDF-":
            return content
    except Exception:
        pass
    return None


def _extract_warranty_with_claude(
    brand: str, model: str, product_name: str, page_text: str
) -> str | None:
    """Use Claude Haiku to extract warranty info from page text."""
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=(
            "Extract warranty info from product page text.\n"
            "Return ONLY the duration as a short string like "
            "'1 Year Limited', '2 Years', 'Limited Lifetime'.\n"
            "If not found, return exactly: NOT FOUND"
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Brand: {brand}\nModel: {model}\n"
                    f"Product: {product_name}\n\nPage text:\n{page_text}"
                ),
            }
        ],
    )
    result = response.content[0].text.strip()
    if result == "NOT FOUND":
        return None
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_manual_and_warranty(
    brand: str, model: str, product_name: str
) -> dict:
    """
    Search the web for a product's manual PDF and warranty length.

    Returns:
        {
            "manual_pdf_bytes": bytes | None,
            "manual_source_url": str | None,
            "warranty_length": str | None,
            "status": "found" | "not_found",
        }
    """
    manual_pdf_bytes: bytes | None = None
    manual_source_url: str | None = None
    warranty_length: str | None = None

    # --- Step 1: Search for manual PDF ---
    query = f"{brand} {model} user manual filetype:pdf"
    search_urls = _search_duckduckgo(query)
    time.sleep(1)

    # Check for direct PDF URLs in results
    for url in search_urls:
        if url.lower().endswith(".pdf"):
            pdf_bytes = _try_download_pdf(url)
            if pdf_bytes:
                manual_pdf_bytes = pdf_bytes
                manual_source_url = url
                break
            time.sleep(1)

    # If no direct PDF, scrape result pages for embedded PDF links
    if manual_pdf_bytes is None:
        for url in search_urls[:5]:
            try:
                resp = requests.get(url, headers=_HEADERS, timeout=15)
                soup = BeautifulSoup(resp.text, "html.parser")
                for a_tag in soup.find_all("a", href=True):
                    href = a_tag["href"]
                    if href.lower().endswith(".pdf"):
                        lower_href = href.lower()
                        if any(
                            kw in lower_href
                            for kw in ["manual", "guide", model.lower()]
                        ):
                            abs_url = urljoin(url, href) if not href.startswith("http") else href
                            pdf_bytes = _try_download_pdf(abs_url)
                            if pdf_bytes:
                                manual_pdf_bytes = pdf_bytes
                                manual_source_url = abs_url
                                break
                if manual_pdf_bytes:
                    break
            except Exception:
                pass
            time.sleep(1)

    # --- Step 2: Search for warranty ---
    warranty_query = f"{brand} {model} warranty"
    warranty_urls = _search_duckduckgo(warranty_query)
    time.sleep(1)

    for url in warranty_urls[:3]:
        page_text = _fetch_page_text(url)
        if page_text:
            warranty_length = _extract_warranty_with_claude(
                brand, model, product_name, page_text
            )
            if warranty_length:
                break
        time.sleep(1)

    status = "found" if manual_pdf_bytes else "not_found"

    return {
        "manual_pdf_bytes": manual_pdf_bytes,
        "manual_source_url": manual_source_url,
        "warranty_length": warranty_length,
        "status": status,
    }
