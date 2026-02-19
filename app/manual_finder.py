from __future__ import annotations

import logging
import os
import re
import time
from urllib.parse import urlparse, parse_qs, unquote, urljoin

import requests
from bs4 import BeautifulSoup
from openai import OpenAI

logger = logging.getLogger("ati.search")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

_MANUFACTURER_DOMAINS = {
    "crestron": "crestron.com",
    "savant": "savant.com",
    "control4": "control4.com",
    "lutron": "lutron.com",
    "sonance": "sonance.com",
    "samsung": "samsung.com",
    "lg": "lg.com",
    "sony": "sony.com",
    "ubiquiti": "ui.com",
    "unifi": "ui.com",
    "wattbox": "snapone.com",
    "snap one": "snapone.com",
    "episode": "snapone.com",
    "binary": "snapone.com",
    "apple": "support.apple.com",
    "sonos": "sonos.com",
    "denon": "denon.com",
    "marantz": "marantz.com",
    "yamaha": "yamaha.com",
    "epson": "epson.com",
    "origin acoustics": "originacoustics.com",
    "atlona": "atlona.com",
    "qsc": "qsc.com",
    "shure": "shure.com",
    "middle atlantic": "middleatlantic.com",
    "araknis": "araknisnetworks.com",
    "parasound": "parasound.com",
    "innovolt": "innovolt.com",
    "surgex": "surgex.com",
    "bose": "bose.com",
    "klipsch": "klipsch.com",
    "jbl": "jbl.com",
    "harman": "harmanpro.com",
    "russound": "russound.com",
    "autonomic": "autonomic.com",
    "seura": "seura.com",
    "leon": "leonspeakers.com",
    "triad": "triadspeakers.com",
    "james loudspeaker": "jamesloudspeaker.com",
    "just add power": "justaddpower.com",
    "access networks": "accessnetworks.com",
    "pakedge": "pakedge.com",
    "ruckus": "ruckuswireless.com",
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
    except Exception as e:
        logger.warning("DuckDuckGo search failed for '%s': %s", query, e)
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

    logger.info("  DDG search '%s' → %d results", query[:60], len(unique))
    return unique[:10]


def _fetch_page_text(url: str) -> str:
    """Fetch a page and return up to 8000 chars of cleaned text."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.debug("  Failed to fetch %s: %s", url[:80], e)
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
            logger.info("  Downloaded PDF (%d bytes) from %s", len(resp.content), url[:80])
            return resp.content
        # Also check for PDF magic bytes as fallback
        content = resp.content
        if content[:5] == b"%PDF-":
            logger.info("  Downloaded PDF via magic bytes (%d bytes) from %s", len(content), url[:80])
            return content
    except Exception as e:
        logger.debug("  PDF download failed for %s: %s", url[:80], e)
    return None


# ---------------------------------------------------------------------------
# Perplexity API (primary search when configured)
# ---------------------------------------------------------------------------

def _get_perplexity_client() -> OpenAI | None:
    """Return a Perplexity client if API key is configured, else None."""
    api_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not api_key:
        return None
    return OpenAI(api_key=api_key, base_url="https://api.perplexity.ai")


def _search_perplexity_for_manual(
    brand: str, model: str, product_name: str
) -> dict:
    """
    Use Perplexity Sonar to find a product manual PDF URL.

    Returns:
        {"manual_url": str | None, "manual_pdf_bytes": bytes | None}
    """
    client = _get_perplexity_client()
    if client is None:
        return {"manual_url": None, "manual_pdf_bytes": None}

    brand_lower = brand.strip().lower()
    domain = _MANUFACTURER_DOMAINS.get(brand_lower)
    domain_hint = f", preferably from {domain}" if domain else ""

    prompt = (
        f"Find the official PDF user manual download link for the {brand} {model} "
        f"({product_name}).{domain_hint}\n\n"
        f"I need a direct URL to the PDF file — not a product page or support page.\n"
        f"Look for the product's user manual, installation guide, or owner's manual.\n\n"
        f"If you find a direct PDF link, state it clearly on its own line as:\n"
        f"PDF_URL: <the full URL>\n\n"
        f"If you cannot find a direct PDF link, state:\n"
        f"PDF_URL: NOT_FOUND"
    )

    try:
        logger.info("  Perplexity manual search for %s %s", brand, model)
        response = client.chat.completions.create(
            model="sonar-pro",
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.choices[0].message.content or ""
        citations = getattr(response, "citations", []) or []

        logger.info("  Perplexity returned %d citations", len(citations))

        # Build candidate URLs from multiple sources
        candidates: list[str] = []

        # Source 1: Explicitly stated PDF_URL in response text
        pdf_url_match = re.search(r"PDF_URL:\s*(https?://\S+)", text)
        if pdf_url_match:
            url = pdf_url_match.group(1).rstrip(")")
            if url != "NOT_FOUND":
                candidates.append(url)

        # Source 2: Citations ending in .pdf
        for c in citations:
            if c.lower().endswith(".pdf"):
                candidates.append(c)

        # Source 3: Citations containing manual/guide keywords in path
        manual_kw = ["manual", "guide", "instruction", "documentation", "user-guide"]
        for c in citations:
            if any(kw in c.lower() for kw in manual_kw) and c not in candidates:
                candidates.append(c)

        # Source 4: All remaining citations (lower priority)
        for c in citations:
            if c not in candidates:
                candidates.append(c)

        # Try downloading PDFs from candidates
        for url in candidates:
            logger.info("  Perplexity candidate: %s", url[:100])
            pdf_bytes = _try_download_pdf(url)
            if pdf_bytes:
                return {"manual_url": url, "manual_pdf_bytes": pdf_bytes}
            time.sleep(0.5)

        # If we found URLs but couldn't download PDFs, scan pages for embedded PDF links
        for url in candidates[:3]:
            if url.lower().endswith(".pdf"):
                continue  # Already tried direct download
            page_pdfs = _scan_page_for_pdf_links(url, model)
            for pdf_url in page_pdfs:
                logger.info("  Perplexity page-scan candidate: %s", pdf_url[:100])
                pdf_bytes = _try_download_pdf(pdf_url)
                if pdf_bytes:
                    return {"manual_url": pdf_url, "manual_pdf_bytes": pdf_bytes}
            time.sleep(0.5)

        # Return the best URL even if we couldn't download it
        if candidates:
            logger.info("  Perplexity found URL but PDF download failed: %s", candidates[0][:100])
            return {"manual_url": candidates[0], "manual_pdf_bytes": None}

        logger.info("  Perplexity found no manual URLs")
        return {"manual_url": None, "manual_pdf_bytes": None}

    except Exception as e:
        logger.warning("  Perplexity manual search failed: %s", e)
        return {"manual_url": None, "manual_pdf_bytes": None}


# ---------------------------------------------------------------------------
# Search query builder (DuckDuckGo fallback)
# ---------------------------------------------------------------------------

def _build_search_queries(brand: str, model: str, product_name: str) -> list[str]:
    """Build an ordered list of search queries to try for finding a product manual."""
    queries = []

    # Query 1: broad manual search (no filetype: — DDG doesn't support it reliably)
    queries.append(f"{brand} {model} user manual PDF")

    # Query 2: manufacturer-specific or fallback
    brand_lower = brand.strip().lower()
    domain = _MANUFACTURER_DOMAINS.get(brand_lower)
    if domain:
        queries.append(f"site:{domain} {model} manual")
    else:
        queries.append(f"{model} installation manual PDF")

    # Query 3: product support / documentation page
    queries.append(f"{brand} {model} product support documentation")

    return queries


def _scan_page_for_pdf_links(page_url: str, model: str) -> list[str]:
    """Scrape a page for links that look like manual PDFs. Returns candidate URLs."""
    candidates: list[str] = []
    try:
        resp = requests.get(page_url, headers=_HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        model_lower = model.lower()
        manual_keywords = ["manual", "guide", "user guide", "instruction", "documentation"]

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            link_text = a_tag.get_text(strip=True).lower()
            lower_href = href.lower()

            is_pdf_link = lower_href.endswith(".pdf")
            has_manual_kw = any(kw in lower_href or kw in link_text for kw in manual_keywords)
            has_model = model_lower in lower_href or model_lower in link_text

            if is_pdf_link and (has_manual_kw or has_model):
                abs_url = urljoin(page_url, href) if not href.startswith("http") else href
                candidates.append(abs_url)
    except Exception:
        pass
    return candidates


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_manual_and_warranty(
    brand: str, model: str, product_name: str
) -> dict:
    """
    Search the web for a product's manual PDF.

    Returns:
        {
            "manual_pdf_bytes": bytes | None,
            "manual_source_url": str | None,
            "status": "found" | "not_found",
        }
    """
    logger.info("Searching for: %s %s", brand, model)
    manual_pdf_bytes: bytes | None = None
    manual_source_url: str | None = None

    # --- Step 1: Try Perplexity first (if configured) ---
    if _get_perplexity_client() is not None:
        logger.info("  Using Perplexity API for manual search...")
        pplx_result = _search_perplexity_for_manual(brand, model, product_name)
        if pplx_result["manual_pdf_bytes"]:
            manual_pdf_bytes = pplx_result["manual_pdf_bytes"]
            manual_source_url = pplx_result["manual_url"]
        elif pplx_result["manual_url"]:
            manual_source_url = pplx_result["manual_url"]

    # --- Step 2: Fall back to DuckDuckGo if no PDF found ---
    if not manual_pdf_bytes:
        logger.info("  Using DuckDuckGo for manual search...")
        queries = _build_search_queries(brand, model, product_name)

        for query_idx, query in enumerate(queries):
            if manual_pdf_bytes:
                break

            logger.info("  DDG Query %d/%d: '%s'", query_idx + 1, len(queries), query)
            search_urls = _search_duckduckgo(query)
            time.sleep(1)

            # Pass 1: Check for direct PDF URLs in results
            for url in search_urls:
                if url.lower().endswith(".pdf"):
                    logger.info("  Trying direct PDF: %s", url[:80])
                    pdf_bytes = _try_download_pdf(url)
                    if pdf_bytes:
                        manual_pdf_bytes = pdf_bytes
                        manual_source_url = url
                        break
                    time.sleep(1)

            if manual_pdf_bytes:
                break

            # Pass 2: Scrape result pages for embedded PDF links
            logger.info("  No direct PDF, scanning result pages...")
            for url in search_urls[:4]:
                pdf_candidates = _scan_page_for_pdf_links(url, model)
                for candidate in pdf_candidates:
                    logger.info("  Trying candidate PDF: %s", candidate[:80])
                    pdf_bytes = _try_download_pdf(candidate)
                    if pdf_bytes:
                        manual_pdf_bytes = pdf_bytes
                        manual_source_url = candidate
                        break
                if manual_pdf_bytes:
                    break
                time.sleep(1)

    status = "found" if manual_pdf_bytes else "not_found"
    logger.info("  Result: %s (manual=%s)", status, "yes" if manual_pdf_bytes else "no")

    return {
        "manual_pdf_bytes": manual_pdf_bytes,
        "manual_source_url": manual_source_url,
        "status": status,
    }


def download_pdf_from_url(url: str) -> bytes | None:
    """
    Download a PDF from a given URL. Used when users manually paste URLs.
    Tries direct download first, then scans the page for embedded PDF links.

    Returns PDF bytes or None.
    """
    logger.info("  Attempting PDF download from user URL: %s", url[:100])

    # Try direct download
    pdf_bytes = _try_download_pdf(url)
    if pdf_bytes:
        return pdf_bytes

    # URL might be an HTML page with a download link — scrape for PDF links
    logger.info("  Direct download failed, scanning page for PDF links...")
    page_pdfs = _scan_page_for_pdf_links(url, "")
    for candidate in page_pdfs:
        logger.info("  Trying page PDF link: %s", candidate[:100])
        pdf_bytes = _try_download_pdf(candidate)
        if pdf_bytes:
            return pdf_bytes

    logger.info("  Could not download PDF from %s", url[:100])
    return None
