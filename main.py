#!/usr/bin/env python3

import asyncio
import hashlib
import json
import re
import urllib.parse
from typing import Any, Dict, List, Optional

from apify import Actor
from apify.storages import KeyValueStore
from crawlee import Request
from playwright.async_api import Browser, BrowserContext, Page, Response, async_playwright


# ---------------------
# Constants / labels
# ---------------------

PRODUCT_LABEL = "PRODUCT"
LISTING_LABEL = "LISTING"

DEFAULT_NAVIGATION_TIMEOUT_MS = 30_000

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.0 Mobile/15E148 Safari/604.1"
)

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/116.0.0.0 Safari/537.36"
)

PRODUCT_URL_MARKERS = (
    "/shop/pdp/",
    "/shop/product/",
    "/view/product/",
    "/product/",
    "/item/",
)


# ---------------------
# Helpers
# ---------------------

def slugify_tiktok_category(cat: str) -> str:
    """Convert a category name/value to a URL-safe slug."""
    cat = (cat or "").strip().lower()
    cat = cat.replace("&", "and")
    cat = re.sub(r"[^a-z0-9]+", "-", cat)
    return cat.strip("-")


def build_search_url_for_keyword(
    keyword: str,
    template: Optional[str] = None,
) -> str:
    """
    Build a discovery URL.

    NOTE:
    TikTok changes its public discovery routes frequently. The default /tag/
    route is only a best-effort discovery source, not a guaranteed TikTok Shop
    search endpoint.
    """
    template = template or "https://www.tiktok.com/tag/{keyword}"
    return template.format(keyword=urllib.parse.quote_plus(keyword))


def normalize_start_items(raw_start_urls: Any) -> List[Dict[str, Any]]:
    """Normalize string/list/dict URL inputs into a common list format."""
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
                continue

            if isinstance(item, dict):
                url = item.get("url")
                if not url:
                    continue

                user_data = item.get("userData") or {}

                if "label" in item:
                    user_data["label"] = item["label"]

                out.append(
                    {
                        "url": url,
                        "userData": user_data,
                    }
                )

    return out


def choose_label_for_url(
    url: str,
    explicit_userdata: Dict[str, Any],
) -> Dict[str, Any]:
    """Classify a URL as a product page or a discovery/listing page."""
    if explicit_userdata and explicit_userdata.get("label"):
        return explicit_userdata

    lower = (url or "").lower()

    if any(marker in lower for marker in PRODUCT_URL_MARKERS):
        return {"label": PRODUCT_LABEL}

    if (
        "/tag/" in lower
        or "/collections/" in lower
        or "/category/" in lower
        or "/search" in lower
        or "/shop/" in lower
    ):
        return {"label": LISTING_LABEL}

    # Direct URLs supplied by the user are safer to treat as products by default.
    return {"label": PRODUCT_LABEL}


def is_product_candidate(url: str) -> bool:
    lower = (url or "").lower()
    return any(marker in lower for marker in PRODUCT_URL_MARKERS)


def ms_timeouts_from_input(raw_timeouts: Dict[str, Any]) -> Dict[str, int]:
    """Convert Actor timeout input values to milliseconds."""
    out: Dict[str, int] = {
        "navigation": DEFAULT_NAVIGATION_TIMEOUT_MS,
        "request": DEFAULT_NAVIGATION_TIMEOUT_MS,
    }

    if not isinstance(raw_timeouts, dict):
        return out

    if "navigation" in raw_timeouts:
        out["navigation"] = int(raw_timeouts["navigation"])
    elif "navigationTimeoutSecs" in raw_timeouts:
        out["navigation"] = int(raw_timeouts["navigationTimeoutSecs"]) * 1000

    if "request" in raw_timeouts:
        out["request"] = int(raw_timeouts["request"])
    elif "requestTimeoutSecs" in raw_timeouts:
        out["request"] = int(raw_timeouts["requestTimeoutSecs"]) * 1000

    return out


def to_playwright_proxy(proxy_url: Optional[str]) -> Optional[Dict[str, str]]:
    """
    Convert an authenticated proxy URL to the Playwright proxy format:
    server + username + password.
    """
    if not proxy_url:
        return None

    parsed = urllib.parse.urlparse(proxy_url)

    if not parsed.scheme or not parsed.hostname:
        raise ValueError("Invalid proxy URL returned by Apify.")

    server = f"{parsed.scheme}://{parsed.hostname}"

    if parsed.port:
        server += f":{parsed.port}"

    proxy: Dict[str, str] = {"server": server}

    if parsed.username:
        proxy["username"] = urllib.parse.unquote(parsed.username)

    if parsed.password:
        proxy["password"] = urllib.parse.unquote(parsed.password)

    return proxy


def stable_key(prefix: str, value: str, extension: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest}.{extension}"


def extract_product_id(url: str) -> Optional[str]:
    """Best-effort extraction of a long numeric TikTok Shop product ID."""
    matches = re.findall(r"(?<!\d)(\d{10,})(?!\d)", url or "")
    return matches[-1] if matches else None


def find_product_schema(value: Any) -> Optional[Dict[str, Any]]:
    """Recursively find a JSON-LD object whose @type is Product."""
    if isinstance(value, dict):
        value_type = value.get("@type")

        if value_type == "Product":
            return value

        if isinstance(value_type, list) and "Product" in value_type:
            return value

        for child in value.values():
            found = find_product_schema(child)
            if found:
                return found

    elif isinstance(value, list):
        for child in value:
            found = find_product_schema(child)
            if found:
                return found

    return None


def normalize_images(raw_images: Any) -> List[str]:
    if isinstance(raw_images, str):
        return [raw_images]

    if isinstance(raw_images, list):
        return [str(item) for item in raw_images if item]

    return []


def parse_json_ld_product(raw_scripts: List[str]) -> Dict[str, Any]:
    """
    Extract generic Product/Offer/Rating information from JSON-LD if the page
    exposes it. Missing fields remain absent instead of being invented.
    """
    result: Dict[str, Any] = {}

    for raw in raw_scripts:
        raw = (raw or "").strip()
        if not raw:
            continue

        try:
            parsed = json.loads(raw)
        except Exception:
            continue

        product = find_product_schema(parsed)
        if not product:
            continue

        if product.get("name"):
            result["name"] = product.get("name")

        if product.get("description"):
            result["description"] = product.get("description")

        if product.get("sku"):
            result["sku"] = str(product.get("sku"))

        brand = product.get("brand")
        if isinstance(brand, dict) and brand.get("name"):
            result["brand"] = brand.get("name")
        elif isinstance(brand, str):
            result["brand"] = brand

        images = normalize_images(product.get("image"))
        if images:
            result["images"] = images

        offers = product.get("offers")

        if isinstance(offers, list) and offers:
            offers = offers[0]

        if isinstance(offers, dict):
            if offers.get("price") is not None:
                result["price"] = offers.get("price")

            if offers.get("priceCurrency"):
                result["currency"] = offers.get("priceCurrency")

            if offers.get("availability"):
                result["availability"] = offers.get("availability")

            if offers.get("url"):
                result["offerUrl"] = offers.get("url")

        aggregate_rating = product.get("aggregateRating")

        if isinstance(aggregate_rating, dict):
            if aggregate_rating.get("ratingValue") is not None:
                result["rating"] = aggregate_rating.get("ratingValue")

            if aggregate_rating.get("reviewCount") is not None:
                result["reviewCount"] = aggregate_rating.get("reviewCount")
            elif aggregate_rating.get("ratingCount") is not None:
                result["reviewCount"] = aggregate_rating.get("ratingCount")

        break

    return result


async def get_meta_content(page: Page, selector: str) -> Optional[str]:
    try:
        locator = page.locator(selector).first
        if await locator.count() == 0:
            return None

        value = await locator.get_attribute("content")
        return value.strip() if value else None
    except Exception:
        return None


async def create_context_for_req(
    browser: Browser,
    accept_language: str,
    proxy_url: Optional[str] = None,
    use_mobile: bool = True,
) -> BrowserContext:
    """Create a Playwright browser context with locale and optional proxy."""
    locale = (accept_language or "de-DE").split(",")[0].strip() or "de-DE"

    context_kwargs: Dict[str, Any] = {
        "user_agent": MOBILE_UA if use_mobile else DESKTOP_UA,
        "locale": locale,
        "java_script_enabled": True,
        "extra_http_headers": {
            "Accept-Language": accept_language
            or "de-DE,de;q=0.9,en;q=0.8",
        },
    }

    playwright_proxy = to_playwright_proxy(proxy_url)

    if playwright_proxy:
        context_kwargs["proxy"] = playwright_proxy

    return await browser.new_context(**context_kwargs)


async def fetch_and_save_response_for_debug(
    page: Page,
    resp: Optional[Response],
    kv_store: KeyValueStore,
    key_prefix: str,
    log,
) -> None:
    """Save the response/page body for debugging."""
    try:
        if resp is None:
            content = await page.content()
            key = f"{key_prefix}.html"
            await kv_store.set_value(
                key,
                content,
                content_type="text/html",
            )
            log.info(f"[DEBUG] saved page.content() to kv://{key}")
            return

        headers = resp.headers or {}
        content_type = headers.get("content-type", "").lower()
        body_text = await resp.text()

        if "html" in content_type or body_text.lstrip().startswith("<"):
            key = f"{key_prefix}.html"
            mime = "text/html"
        elif (
            "json" in content_type
            or body_text.lstrip().startswith("{")
            or body_text.lstrip().startswith("[")
        ):
            key = f"{key_prefix}.json"
            mime = "application/json"
        else:
            key = f"{key_prefix}.txt"
            mime = "text/plain"

        await kv_store.set_value(
            key,
            body_text,
            content_type=mime,
        )

        log.info(
            f"[DEBUG] saved response to kv://{key} "
            f"(status={resp.status})"
        )
    except Exception as exc:
        log.warning(f"[DEBUG] Failed saving debug response: {exc}")


async def auto_scroll_page(
    page: Page,
    scroll_step: int = 1200,
    max_scrolls: int = 12,
    wait_time_ms: int = 800,
    log=None,
) -> None:
    """Scroll down to trigger lazy-loaded content."""
    if log:
        log.info(
            f"[SCROLL] step={scroll_step}px max={max_scrolls}"
        )

    try:
        last_height = await page.evaluate(
            "() => document.body ? document.body.scrollHeight : 0"
        )
    except Exception:
        return

    for index in range(max_scrolls):
        await page.evaluate(
            f"window.scrollBy(0, {scroll_step});"
        )
        await page.wait_for_timeout(wait_time_ms)

        new_height = await page.evaluate(
            "() => document.body ? document.body.scrollHeight : 0"
        )

        if new_height == last_height:
            if log:
                log.info(
                    f"[SCROLL] no more new content after "
                    f"{index + 1} scrolls."
                )
            break

        last_height = new_height

    if log:
        log.info(
            f"[SCROLL] finished at height={last_height}"
        )


# ---------------------
# Product task
# ---------------------

async def process_product_task(
    browser: Browser,
    req: Request,
    proxy_url: Optional[str],
    accept_language: str,
    region: str,
    timeouts: Dict[str, Any],
    capture_screenshots: bool,
    kv_store: KeyValueStore,
    log,
    debug: bool = False,
) -> None:
    url = req.url
    log.info(f"[PRODUCT] {url}")

    context: Optional[BrowserContext] = None
    page: Optional[Page] = None
    response: Optional[Response] = None

    try:
        context = await create_context_for_req(
            browser,
            accept_language,
            proxy_url,
            use_mobile=True,
        )
        page = await context.new_page()

        response = await page.goto(
            url,
            timeout=timeouts.get(
                "navigation",
                DEFAULT_NAVIGATION_TIMEOUT_MS,
            ),
            wait_until="domcontentloaded",
        )

        await page.wait_for_timeout(2_000)

        final_url = page.url
        status = response.status if response else None

    try:
            page_title = await page.title()
        except Exception:
            page_title = None

        # Detect TikTok anti-bot / security challenge
        challenge_titles = {
            "security check",
            "verify to continue",
            "verification",
            "captcha",
        }

        normalized_title = (page_title or "").strip().lower()

        if normalized_title in challenge_titles:
            log.warning(
                f"[PRODUCT] TikTok security challenge detected: {url}"
            )

            await Actor.push_data({
                "type": "blocked",
                "sourceUrl": url,
                "finalUrl": page.url,
                "productId": extract_product_id(url),
                "region": region,
                "httpStatus": status,
                "reason": "TIKTOK_SECURITY_CHECK",
                "title": page_title,
            })

            if debug:
                key = stable_key(
                    "security-check",
                    url,
                    "html",
                )
                await kv_store.set_value(
                    key,
                    await page.content(),
                    content_type="text/html",
                )

            if capture_screenshots:
                key = stable_key(
                    "security-check",
                    url,
                    "png",
                )
                await kv_store.set_value(
                    key,
                    await page.screenshot(full_page=True),
                    content_type="image/png",
                )

            return

        og_title = await get_meta_content(
            page,
            'meta[property="og:title"]',
        )
        description = (
            await get_meta_content(
                page,
                'meta[property="og:description"]',
            )
            or await get_meta_content(
                page,
                'meta[name="description"]',
            )
        )

        try:
            canonical = await page.locator(
                'link[rel="canonical"]'
            ).first.get_attribute("href")
        except Exception:
            canonical = None

        try:
            raw_json_ld = await page.locator(
                'script[type="application/ld+json"]'
            ).all_text_contents()
        except Exception:
            raw_json_ld = []

        structured = parse_json_ld_product(raw_json_ld)

        record: Dict[str, Any] = {
            "type": "product",
            "sourceUrl": url,
            "finalUrl": final_url,
            "productId": extract_product_id(final_url)
            or extract_product_id(url),
            "region": region,
            "httpStatus": status,
            "title": (
                structured.get("name")
                or og_title
                or page_title
            ),
            "description": (
                structured.get("description")
                or description
            ),
            "canonicalUrl": canonical,
        }

        for field in (
            "sku",
            "brand",
            "images",
            "price",
            "currency",
            "availability",
            "rating",
            "reviewCount",
            "offerUrl",
        ):
            if structured.get(field) is not None:
                record[field] = structured[field]

        await Actor.push_data(record)

        log.info(
            f"[PRODUCT] pushed data for {url}: "
            f"title={record.get('title')!r}, "
            f"status={status}, final_url={final_url}"
        )

        if debug:
            key_prefix = stable_key(
                "debug-product",
                url,
                "response",
            ).rsplit(".", 1)[0]

            await fetch_and_save_response_for_debug(
                page,
                response,
                kv_store,
                key_prefix,
                log,
            )

        if capture_screenshots:
            screenshot = await page.screenshot(
                full_page=True
            )
            key = stable_key(
                "screenshot-product",
                url,
                "png",
            )
            await kv_store.set_value(
                key,
                screenshot,
                content_type="image/png",
            )
            log.info(
                f"[PRODUCT] screenshot saved to kv://{key}"
            )

    except Exception as exc:
        log.warning(f"[PRODUCT] error {url}: {exc}")

        # Save useful failure diagnostics instead of crashing the entire Actor.
        error_record = {
            "type": "product_error",
            "sourceUrl": url,
            "region": region,
            "error": str(exc),
        }

        if page is not None:
            error_record["finalUrl"] = page.url

            if debug:
                try:
                    key = stable_key(
                        "debug-product-error",
                        url,
                        "html",
                    )
                    await kv_store.set_value(
                        key,
                        await page.content(),
                        content_type="text/html",
                    )
                except Exception:
                    pass

            if capture_screenshots:
                try:
                    key = stable_key(
                        "screenshot-product-error",
                        url,
                        "png",
                    )
                    await kv_store.set_value(
                        key,
                        await page.screenshot(full_page=True),
                        content_type="image/png",
                    )
                except Exception:
                    pass

        await Actor.push_data(error_record)

    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass

        if context is not None:
            try:
                await context.close()
            except Exception:
                pass


# ---------------------
# Listing / discovery task
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
) -> None:
    url = req.url
    log.info(f"[LISTING] {url}")

    context: Optional[BrowserContext] = None
    page: Optional[Page] = None
    response: Optional[Response] = None

    try:
        context = await create_context_for_req(
            browser,
            accept_language,
            proxy_url,
            use_mobile=True,
        )
        page = await context.new_page()

        response = await page.goto(
            url,
            timeout=timeouts.get(
                "navigation",
                DEFAULT_NAVIGATION_TIMEOUT_MS,
            ),
            wait_until="domcontentloaded",
        )

        await page.wait_for_timeout(1_500)
        await auto_scroll_page(page, log=log)

        if debug:
            key_prefix = stable_key(
                "debug-listing",
                url,
                "response",
            ).rsplit(".", 1)[0]

            await fetch_and_save_response_for_debug(
                page,
                response,
                kv_store,
                key_prefix,
                log,
            )

        anchors = await page.query_selector_all("a")
        candidates: List[str] = []

        for anchor in anchors:
            href = await anchor.get_attribute("href")

            if not href:
                continue

            href = href.strip()

            if (
                href.startswith("javascript:")
                or href.startswith("#")
            ):
                continue

            absolute_url = urllib.parse.urljoin(
                page.url,
                href,
            )

            if is_product_candidate(absolute_url):
                candidates.append(absolute_url)

        unique_candidates = list(
            dict.fromkeys(candidates)
        )

        for candidate in unique_candidates:
            await request_queue.add_request(
                Request.from_url(
                    candidate,
                    label=PRODUCT_LABEL,
                )
            )

            log.info(
                f"[LISTING] queued product: {candidate}"
            )

        await Actor.push_data(
            {
                "type": "listing",
                "sourceUrl": url,
                "finalUrl": page.url,
                "region": region,
                "httpStatus": (
                    response.status
                    if response
                    else None
                ),
                "productsDiscovered": len(
                    unique_candidates
                ),
            }
        )

        if not unique_candidates:
            log.info(
                f"[LISTING] no product candidates found "
                f"on {url}"
            )

    except Exception as exc:
        log.warning(f"[LISTING] error {url}: {exc}")

        await Actor.push_data(
            {
                "type": "listing_error",
                "sourceUrl": url,
                "region": region,
                "error": str(exc),
                "finalUrl": (
                    page.url
                    if page is not None
                    else None
                ),
            }
        )

    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass

        if context is not None:
            try:
                await context.close()
            except Exception:
                pass


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
) -> None:
    log.info(f"Worker {worker_id} started")

    while True:
        req = await request_queue.fetch_next_request()

        if req is None:
            try:
                if await request_queue.is_finished():
                    break
            except Exception:
                break

            await asyncio.sleep(0.25)
            continue

        label = req.label or PRODUCT_LABEL

        proxy_url: Optional[str] = None

        if proxy_configuration is not None:
            try:
                proxy_url = await proxy_configuration.new_url()
            except Exception as exc:
                log.warning(
                    f"[PROXY] Could not obtain proxy URL: {exc}"
                )

        try:
            if label == PRODUCT_LABEL:
                await process_product_task(
                    browser,
                    req,
                    proxy_url,
                    accept_language,
                    region,
                    timeouts,
                    capture_screenshots,
                    kv_store,
                    log,
                    debug,
                )
            else:
                await process_listing_task(
                    browser,
                    req,
                    proxy_url,
                    accept_language,
                    region,
                    timeouts,
                    request_queue,
                    log,
                    kv_store,
                    debug,
                )

        finally:
            try:
                await request_queue.mark_request_as_handled(
                    req
                )
            except Exception as exc:
                log.warning(
                    f"[QUEUE] Could not mark request handled: {exc}"
                )

    log.info(f"Worker {worker_id} finished")


# ---------------------
# Main
# ---------------------

async def main() -> None:
    async with Actor:
        log = Actor.log
        input_data = await Actor.get_input() or {}

        try:
            log.info(
                "Actor input: "
                + json.dumps(
                    input_data,
                    ensure_ascii=False,
                )
            )
        except Exception:
            pass

        raw_start = (
            input_data.get("startUrls")
            or input_data.get("start_urls")
            or input_data.get("productUrls")
            or []
        )

        start_items = normalize_start_items(
            raw_start
        )

        # Optional seller profile discovery.
        seller_handles = (
            input_data.get("sellerHandles")
            or []
        )

        if isinstance(seller_handles, str):
            seller_handles = [seller_handles]

        for handle in seller_handles:
            clean_handle = str(handle).strip().lstrip("@")
            if not clean_handle:
                continue

            start_items.append(
                {
                    "url": (
                        "https://www.tiktok.com/"
                        f"@{clean_handle}"
                    ),
                    "userData": {
                        "label": LISTING_LABEL,
                    },
                }
            )

        # Optional category discovery.
        tiktok_categories = (
            input_data.get("tiktokCategories")
            or []
        )

        category_urls = (
            input_data.get("categoryUrls")
            or []
        )

        for category in tiktok_categories:
            slug = slugify_tiktok_category(
                str(category)
            )

            if not slug:
                continue

            start_items.append(
                {
                    "url": (
                        "https://www.tiktok.com/tag/"
                        f"{slug}"
                    ),
                    "userData": {
                        "label": LISTING_LABEL,
                    },
                }
            )

        for url in category_urls:
            if not url:
                continue

            start_items.append(
                {
                    "url": str(url),
                    "userData": {
                        "label": LISTING_LABEL,
                    },
                }
            )

        # Optional keyword discovery.
        keywords = (
            input_data.get("keywords")
            or input_data.get("keyword")
            or input_data.get("searchKeywords")
        )

        search_template = input_data.get(
            "searchUrlTemplate"
        )

        if keywords:
            if isinstance(keywords, str):
                keywords = [keywords]

            for keyword in keywords:
                if not keyword:
                    continue

                start_items.append(
                    {
                        "url": build_search_url_for_keyword(
                            str(keyword),
                            template=search_template,
                        ),
                        "userData": {
                            "label": LISTING_LABEL,
                        },
                    }
                )

        if not start_items:
            log.warning(
                "No product URLs, seller handles, "
                "keywords, categories, or category URLs "
                "were provided."
            )
            return

        timeouts = ms_timeouts_from_input(
            input_data.get("timeouts", {})
        )

        accept_language = input_data.get(
            "acceptLanguage",
            "de-DE,de;q=0.9,en;q=0.8",
        )

        region = input_data.get(
            "region",
            "DE",
        )

        capture_screenshots = bool(
            input_data.get(
                "captureScreenshots",
                False,
            )
        )

        debug = bool(
            input_data.get(
                "debug",
                False,
            )
        )

        proxy_configuration = (
            await Actor.create_proxy_configuration(
                actor_proxy_input=input_data.get(
                    "proxyConfiguration"
                )
            )
        )

        kv_store = await KeyValueStore.open()
        request_queue = (
            await Actor.open_request_queue()
        )

        added = 0

        for item in start_items:
            url = item.get("url")
            user_data = (
                item.get("userData", {})
                or {}
            )

            if not url:
                continue

            if not user_data.get("label"):
                user_data = choose_label_for_url(
                    url,
                    user_data,
                )

            label = (
                user_data.get("label")
                or PRODUCT_LABEL
            )

            await request_queue.add_request(
                Request.from_url(
                    url,
                    label=label,
                )
            )

            added += 1

            log.info(
                f"Queued start URL: {url} "
                f"(label={label})"
            )

        log.info(
            f"Added {added} start requests."
        )

        if added == 0:
            log.warning(
                "No valid requests were added."
            )
            return

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )

            worker_count = max(
                1,
                int(
                    input_data.get(
                        "maxConcurrency",
                        input_data.get(
                            "concurrency",
                            1,
                        ),
                    )
                ),
            )

            workers = [
                worker_loop(
                    worker_id=index,
                    browser=browser,
                    request_queue=request_queue,
                    proxy_configuration=proxy_configuration,
                    accept_language=accept_language,
                    region=region,
                    timeouts=timeouts,
                    capture_screenshots=capture_screenshots,
                    kv_store=kv_store,
                    log=log,
                    debug=debug,
                )
                for index in range(
                    worker_count
                )
            ]

            await asyncio.gather(*workers)
            await browser.close()

        log.info("Run finished.")


if __name__ == "__main__":
    asyncio.run(main())
