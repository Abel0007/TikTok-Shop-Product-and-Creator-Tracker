#!/usr/bin/env python3
import asyncio
import json
import urllib.parse
import re
from typing import Any, Dict, List, Optional

from apify import Actor, ProxyConfiguration
from crawlee import Request
from apify.storages import KeyValueStore
from playwright.async_api import async_playwright, Browser, Page, Response

# Labels
PRODUCT_LABEL = "PRODUCT"
LISTING_LABEL = "LISTING"

DEFAULT_NAVIGATION_TIMEOUT_MS = 30000
MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"


class LimitsTracker:
    def __init__(self):
        self.limits = {}


# ---------------------
# Helpers
# ---------------------
def slugify_tiktok_category(cat: str) -> str:
    """Convert 'Fashion & Accessories' → 'fashion-and-accessories' for TikTok tag URL."""
    cat = cat.lower()
    cat = cat.replace("&", "and")
    cat = re.sub(r"[^a-z0-9]+", "-", cat)
    return cat.strip("-")


def build_search_url_for_keyword(keyword: str, template: Optional[str] = None) -> str:
    """Default to /tag/ pages because /search?q= returns JSON."""
    template = template or "https://www.tiktok.com/tag/{keyword}"
    return template.format(keyword=urllib.parse.quote_plus(keyword))


def normalize_start_items(raw_start_urls: Any) -> List[Dict[str, Any]]:
    """Normalize various forms of startUrls/productUrls into list of dicts."""
    out: List[Dict[str, Any]] = []
    if not raw_start_urls:
        return out
    if isinstance(raw_start_urls, str):
        out.append({"url": raw_start_urls})
        return out
    if isinstance(raw_start_urls, list):
        for item in raw_start_urls:
            if isinstance(item, str):
                out.append({"url": item})
            elif isinstance(item, dict):
                url = item.get("url")
                if url:
                    ud = item.get("userData") or {}
                    if "label" in item:
                        ud["label"] = item["label"]
                    out.append({"url": url, "userData": ud})
    return out


def choose_label_for_url(url: str, explicit_userdata: Dict[str, Any]) -> Dict[str, Any]:
    if explicit_userdata and explicit_userdata.get("label"):
        return explicit_userdata

    lower = url.lower()

    # TikTok Shop product pages
    if (
        "/shop/pdp/" in lower
        or "/shop/product/" in lower
        or "/product/" in lower
        or "/item/" in lower
    ):
        return {"label": PRODUCT_LABEL}

    # Listing / discovery pages
    if (
        "/tag/" in lower
        or "/collections/" in lower
        or "/shop/" in lower
    ):
        return {"label": LISTING_LABEL}

    return {"label": PRODUCT_LABEL}


def ms_timeouts_from_input(raw_timeouts: Dict[str, Any]) -> Dict[str, int]:
    out = {}
    if not isinstance(raw_timeouts, dict):
        out["navigation"] = DEFAULT_NAVIGATION_TIMEOUT_MS
        return out

    if "navigation" in raw_timeouts:
        out["navigation"] = int(raw_timeouts["navigation"])
    elif "navigationTimeoutSecs" in raw_timeouts:
        out["navigation"] = int(raw_timeouts["navigationTimeoutSecs"]) * 1000
    else:
        out["navigation"] = DEFAULT_NAVIGATION_TIMEOUT_MS

    return out


def to_playwright_proxy(proxy_url: str):
    if not proxy_url:
        return None

    parsed = urllib.parse.urlparse(proxy_url)

    server = f"{parsed.scheme}://{parsed.hostname}"

    if parsed.port:
        server += f":{parsed.port}"

    proxy = {
        "server": server,
    }

    if parsed.username:
        proxy["username"] = urllib.parse.unquote(parsed.username)

    if parsed.password:
        proxy["password"] = urllib.parse.unquote(parsed.password)

    return proxy


async def create_context_for_req(
    browser: Browser,
    accept_language: str,
    proxy_url: Optional[str] = None,
    use_mobile: bool = True,
):
    locale = (accept_language or "de-DE").split(",")[0].strip()

    context_kwargs = {
        "user_agent": MOBILE_UA if use_mobile else DESKTOP_UA,
        "locale": locale,
        "java_script_enabled": True,
        "extra_http_headers": {
            "Accept-Language": accept_language
            or "de-DE,de;q=0.9,en;q=0.8"
        },
    }

    if proxy_url:
        context_kwargs["proxy"] = to_playwright_proxy(proxy_url)

    return await browser.new_context(**context_kwargs)


async def fetch_and_save_response_for_debug(page: Page, resp: Optional[Response], kv_store: KeyValueStore, key_prefix: str, log):
    try:
        if resp is None:
            content = await page.content()
            key = f"{key_prefix}.html"
            await kv_store.set_value(key, content, content_type="text/html")
            log.info(f"[DEBUG] saved page.content() to kv://{key}")
            return

        headers = resp.headers or {}
        ct = headers.get("content-type", "").lower()
        status = resp.status
        body_text = await resp.text()

        if "html" in ct or body_text.strip().startswith("<"):
            key = f"{key_prefix}.html"
            await kv_store.set_value(key, body_text, content_type="text/html")
        elif "json" in ct or body_text.strip().startswith("{") or body_text.strip().startswith("["):
            key = f"{key_prefix}.json"
            await kv_store.set_value(key, body_text, content_type="application/json")
        else:
            key = f"{key_prefix}.txt"
            await kv_store.set_value(key, body_text, content_type="text/plain")

        log.info(f"[DEBUG] saved response to kv://{key} (status={status})")
    except Exception as e:
        log.warning(f"[DEBUG] Failed saving debug response: {e}")


async def auto_scroll_page(page, scroll_step=1200, max_scrolls=12, wait_time_ms=800, log=None):
    """Scroll down to load dynamic content."""
    if log:
        log.info(f"[SCROLL] step={scroll_step}px max={max_scrolls}")
    last_height = await page.evaluate("() => document.body.scrollHeight")
    for i in range(max_scrolls):
        await page.evaluate(f"window.scrollBy(0, {scroll_step});")
        await page.wait_for_timeout(wait_time_ms)
        new_height = await page.evaluate("() => document.body.scrollHeight")
        if new_height == last_height:
            if log:
                log.info(f"[SCROLL] no more new content after {i+1} scrolls.")
            break
        last_height = new_height
    if log:
        log.info(f"[SCROLL] finished at height={last_height}")


# ---------------------
# Tasks
# ---------------------
async def process_listing_task(
    browser: Browser,
    req: Request,
    proxy_url: Optional[str],
    accept_language: str,
    region: str,
    timeouts: Dict[str, Any],
    request_queue,
    log,
    kv_store: KeyValueStore,
    debug: bool = False,
):
    url = req.url
    log.info(f"[PRODUCT] {url}")
    context = await create_context_for_req(browser, accept_language, proxy_url, use_mobile=True)
    page: Page = await context.new_page()
    try:
        await page.goto(url, timeout=timeouts.get("navigation", DEFAULT_NAVIGATION_TIMEOUT_MS), wait_until="networkidle")
        await page.wait_for_timeout(500)
        title = None
        try:
            title = await page.title()
        except Exception:
            pass
        await Actor.push_data({"url": url, "title": title})
        log.info(f"[PRODUCT] pushed data for {url}: {title}")
        if capture_screenshots:
            png = await page.screenshot(full_page=True)
            key = f"screenshots/{urllib.parse.quote_plus(url)}.png"
            await kv_store.set_value(key, png, content_type="image/png")
    except Exception as e:
        log.warning(f"[PRODUCT] error {url}: {e}")
    finally:
        await page.close()
        await context.close()


async def process_listing_task(
    browser: Browser,
    req: Dict[str, Any],
    proxy_url: Optional[str],
    accept_language: str,
    region: str,
    timeouts: Dict[str, Any],
    request_queue,
    log,
    kv_store: KeyValueStore,
    debug: bool = False,
):
    url = req.get("url")
    log.info(f"[LISTING] {url}")
    context = await create_context_for_req(browser, accept_language, proxy_url, use_mobile=True)
    page: Page = await context.new_page()
    resp = None
    discovered = 0
    try:
        resp = await page.goto(url, timeout=timeouts.get("navigation", DEFAULT_NAVIGATION_TIMEOUT_MS), wait_until="networkidle")
        await auto_scroll_page(page, log=log)

        if debug:
            await fetch_and_save_response_for_debug(page, resp, kv_store, f"debug/listing-{urllib.parse.quote_plus(url)}", log)

        anchors = await page.query_selector_all("a")
        hrefs = []
        for a in anchors:
            href = await a.get_attribute("href")
            if not href:
                continue
            href = href.strip()
            if href.startswith("/"):
                href = urllib.parse.urljoin(page.url, href)
            if href.startswith("javascript:") or href.startswith("#"):
                continue
            hrefs.append(href)

        product_candidates = []
        for h in hrefs:
            hl = h.lower()
            if "/product/" in hl or "/item/" in hl or "/shop/" in hl or "/video/" in hl or "tiktok.com/@" in hl:
                product_candidates.append(h)

        unique = list(dict.fromkeys(product_candidates))
        for candidate in unique:
            await request_queue.add_request({"url": candidate, "userData": {"label": PRODUCT_LABEL}})
            discovered += 1
            log.info(f"[LISTING] queued: {candidate}")

        if discovered == 0:
            log.info(f"[LISTING] no product candidates found on {url}")
    except Exception as e:
        log.warning(f"[LISTING] error {url}: {e}")
    finally:
        await page.close()
        await context.close()


# ---------------------
# Worker
# ---------------------
async def worker_loop(
    worker_id: int,
    browser: Browser,
    request_queue,
    proxy_configuration,
    accept_language: str,
    region: str,
    timeouts: Dict[str, Any],
    capture_screenshots: bool,
    kv_store: KeyValueStore,
    log,
    debug: bool,
):
    log.info(f"Worker {worker_id} started")
    while True:
        req = await request_queue.fetch_next_request()
        if not req:
            break
        label = (req.get("userData") or {}).get("label")

        proxy_url = None
        try:
            proxy_url = await proxy_configuration.new_url() if proxy_configuration else None
        except Exception:
            proxy_url = None

        if label == PRODUCT_LABEL:
            await process_product_task(browser, req, proxy_url, accept_language, region, timeouts, capture_screenshots, kv_store, log)
        else:
            await process_listing_task(browser, req, proxy_url, accept_language, region, timeouts, request_queue, log, kv_store, debug)

        try:
            await request_queue.mark_request_as_handled(req)
        except Exception:
            pass


# ---------------------
# Main entry
# ---------------------
async def main():
    async with Actor:
        log = Actor.log
        input_data = await Actor.get_input() or {}
        try:
            log.info("Actor input: " + json.dumps(input_data))
        except Exception:
            pass

        # Accept multiple field names for start URLs
        raw_start = (
            input_data.get("startUrls")
            or input_data.get("start_urls")
            or input_data.get("productUrls")
            or []
        )
        keywords = input_data.get("keywords") or input_data.get("keyword") or input_data.get("searchKeywords")
        search_template = input_data.get("searchUrlTemplate")
        start_items = normalize_start_items(raw_start)

        # Handle TikTok categories from dropdown and categoryUrls manually
        tiktok_categories = input_data.get("tiktokCategories") or []
        category_urls = input_data.get("categoryUrls") or []

        for cat in tiktok_categories:
            slug = slugify_tiktok_category(cat)
            start_items.append({
                "url": f"https://www.tiktok.com/tag/{slug}",
                "userData": {"label": LISTING_LABEL}
            })

        for url in category_urls:
            start_items.append({
                "url": url,
                "userData": {"label": LISTING_LABEL}
            })

        # Handle keywords → /tag/ URLs
        if keywords:
            if isinstance(keywords, str):
                keywords = [keywords]
            for kw in keywords:
                start_items.append({
                    "url": build_search_url_for_keyword(kw, template=search_template),
                    "userData": {"label": LISTING_LABEL}
                })

        if not start_items:
            log.warning("No start URLs or keywords or categories provided.")
            return

        timeouts = ms_timeouts_from_input(input_data.get("timeouts", {}))
        accept_language = input_data.get("acceptLanguage", "en-US")
        region = input_data.get("region", "US")
        capture_screenshots = input_data.get("captureScreenshots", False)
        debug = bool(input_data.get("debug", False))

        proxy_configuration = await Actor.create_proxy_configuration(
    actor_proxy_input=input_data.get("proxyConfiguration")
)

        kv_store = await KeyValueStore.open()
        request_queue = await Actor.open_request_queue()

        added = 0
        for item in start_items:
            url = item.get("url")
            userData = item.get("userData", {}) or {}
            if not userData.get("label"):
                userData = choose_label_for_url(url, userData)
            await request_queue.add_request({"url": url, "userData": userData})
            added += 1
            log.info(f"Queued start URL: {url} (label={userData.get('label')})")
        log.info(f"Added {added} start requests.")

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=Actor.config.headless,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            worker_count = int(input_data.get("maxConcurrency", input_data.get("concurrency", 3)))
            workers = [
                worker_loop(
                    i,
                    browser,
                    request_queue,
                    proxy_configuration,
                    accept_language,
                    region,
                    timeouts,
                    capture_screenshots,
                    kv_store,
                    log,
                    debug,
                )
                for i in range(worker_count)
            ]
            await asyncio.gather(*workers)
            await browser.close()

        log.info("Run finished.")


if __name__ == "__main__":
    asyncio.run(main())
