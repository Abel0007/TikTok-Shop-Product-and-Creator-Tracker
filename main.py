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

DEFAULT_FALLBACK_ACTOR_ID = "jungle_synthesizer/tiktok-shop-product-detail-scraper"
DEFAULT_SEARCH_ACTOR_ID = "getanyapi/tiktok-shop-scraper"
DEFAULT_DETAIL_ACTOR_ID = "cunning_soil/tiktok-shop-product-scraper-mobile-api"


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
# Search provider (TikTok Shop DE / multi-market)
# ---------------------

def as_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        cleaned = value.strip().replace("%", "").replace(" ", "")
        if not cleaned:
            return None

        # Handle common German decimal notation only when unambiguous.
        if "," in cleaned and "." not in cleaned:
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")

        try:
            return float(cleaned)
        except ValueError:
            return None

    return None


def normalize_search_product(
    raw: Dict[str, Any],
    query: str,
    provider_actor_id: str,
    region: str,
) -> Dict[str, Any]:
    price = as_number(raw.get("price"))
    original_price = as_number(raw.get("originalPrice"))

    discount_percent: Optional[float] = None

    if (
        price is not None
        and original_price is not None
        and original_price > 0
        and original_price > price
    ):
        discount_percent = round(
            ((original_price - price) / original_price) * 100,
            2,
        )
    else:
        raw_discount = as_number(
            raw.get("discountPercent")
            if raw.get("discountPercent") is not None
            else raw.get("discount")
        )
        if raw_discount is not None and 0 <= raw_discount <= 100:
            discount_percent = round(raw_discount, 2)

    return {
        "type": "product",
        "source": raw.get("source") or provider_actor_id,
        "searchQuery": query,
        "region": region.upper(),
        "productId": str(raw.get("productId")) if raw.get("productId") is not None else None,
        "title": raw.get("title"),
        "description": (
            raw.get("description")
            or raw.get("productDescription")
            or raw.get("product_description")
        ),
        "price": price,
        "originalPrice": original_price,
        "discountPercent": discount_percent,
        "currency": raw.get("currency"),
        "soldCount": raw.get("soldCount"),
        "rating": raw.get("rating"),
        "reviewCount": raw.get("reviewCount"),
        "shopName": raw.get("shopName"),
        "sellerId": str(raw.get("sellerId")) if raw.get("sellerId") is not None else None,
        "image": raw.get("image"),
        "productUrl": raw.get("url"),
    }


def product_passes_filters(
    product: Dict[str, Any],
    only_discounted: bool,
    min_discount: float,
    min_sold: int,
    min_rating: float,
) -> bool:
    discount = as_number(product.get("discountPercent")) or 0.0
    sold = as_number(product.get("soldCount")) or 0.0
    rating = as_number(product.get("rating")) or 0.0

    if only_discounted and discount <= 0:
        return False

    if discount < min_discount:
        return False

    if sold < min_sold:
        return False

    if rating < min_rating:
        return False

    return True


def sort_products(
    products: List[Dict[str, Any]],
    sort_by: str,
) -> List[Dict[str, Any]]:
    sort_by = (sort_by or "discount").lower()

    if sort_by == "sold":
        key = lambda p: (
            as_number(p.get("soldCount")) or 0.0,
            as_number(p.get("discountPercent")) or 0.0,
        )
    elif sort_by == "rating":
        key = lambda p: (
            as_number(p.get("rating")) or 0.0,
            as_number(p.get("soldCount")) or 0.0,
        )
    else:
        key = lambda p: (
            as_number(p.get("discountPercent")) or 0.0,
            as_number(p.get("soldCount")) or 0.0,
        )

    return sorted(products, key=key, reverse=True)


async def call_search_provider(
    query: str,
    region: str,
    max_items: int,
    search_actor_id: str,
    log,
) -> tuple[List[Dict[str, Any]], Optional[str]]:
    search_input = {
        "query": query,
        "country": region.upper(),
        "maxItems": max_items,
    }

    try:
        log.info(
            f"[SEARCH] Calling {search_actor_id} "
            f"query={query!r}, country={region.upper()}, maxItems={max_items}"
        )

        run = await Actor.call(
            actor_id=search_actor_id,
            run_input=search_input,
        )

        if run is None:
            return [], "Search Actor failed to start."

        run_client = Actor.apify_client.run(run.id)
        dataset_client = run_client.dataset()
        page = await dataset_client.list_items(limit=max_items)
        raw_items = page.items or []

        products = [
            normalize_search_product(
                raw=item,
                query=query,
                provider_actor_id=search_actor_id,
                region=region,
            )
            for item in raw_items
            if isinstance(item, dict)
        ]

        log.info(
            f"[SEARCH] {query!r}: received {len(products)} products"
        )

        return products, None

    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        log.warning(f"[SEARCH] Failed for {query!r}: {message}")
        return [], message


# ---------------------
# External product fallback
# ---------------------

def normalize_fallback_product(
    raw: Dict[str, Any],
    source_url: str,
    fallback_actor_id: str,
) -> Dict[str, Any]:
    """
    Normalize the documented output of the fallback TikTok Shop product Actor
    to the stable camelCase schema used by this Actor.
    """
    return {
        "type": "product",
        "source": fallback_actor_id,
        "fallbackUsed": True,
        "sourceUrl": source_url,
        "productId": raw.get("product_id"),
        "title": raw.get("product_name"),
        "description": raw.get("description"),
        "brand": raw.get("brand"),
        "categoryPath": raw.get("category_path"),
        "price": raw.get("price"),
        "originalPrice": raw.get("original_price"),
        "currency": raw.get("currency"),
        "discountPercent": raw.get("discount_percent"),
        "soldCount": raw.get("sold_count"),
        "availableQuantity": raw.get("available_quantity"),
        "inStock": raw.get("in_stock"),
        "rating": raw.get("rating"),
        "reviewCount": raw.get("review_count"),
        "images": raw.get("images") or [],
        "videos": raw.get("videos") or [],
        "variants": raw.get("variants") or [],
        "shopName": raw.get("shop_name"),
        "shopId": raw.get("shop_id"),
        "productUrl": raw.get("product_url"),
        "region": raw.get("region"),
        "creatorVideos": raw.get("creator_videos") or [],
        "creatorVideosCount": raw.get("creator_videos_count"),
        "scrapedAt": raw.get("scrapedAt"),
    }


async def call_product_fallback(
    source_url: str,
    region: str,
    include_creator_videos: bool,
    deep_creator_videos: bool,
    fallback_actor_id: str,
    log,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Call a second Apify Actor only when direct Playwright access is blocked.

    Returns:
        (normalized_product, None) on success
        (None, error_message) on failure
    """
    item = extract_product_id(source_url) or source_url
    country = (region or "DE").strip().lower()

    fallback_input = {
        "items": [item],
        "country": country,
        "maxItems": 1,
        "includeCreatorVideos": bool(include_creator_videos),
        "deepCreatorVideos": bool(deep_creator_videos),
    }

    try:
        log.info(
            f"[FALLBACK] Calling {fallback_actor_id} "
            f"for item={item}, country={country}"
        )

        run = await Actor.call(
            actor_id=fallback_actor_id,
            run_input=fallback_input,
        )

        if run is None:
            return None, "Fallback Actor failed to start."

        run_client = Actor.apify_client.run(run.id)
        dataset_client = run_client.dataset()
        page = await dataset_client.list_items(limit=1)

        items = page.items or []

        if not items:
            return None, "Fallback Actor returned no product records."

        raw = items[0]

        if not isinstance(raw, dict):
            return None, "Fallback Actor returned an unexpected result type."

        product = normalize_fallback_product(
            raw=raw,
            source_url=source_url,
            fallback_actor_id=fallback_actor_id,
        )

        if not product.get("productId") and not product.get("title"):
            return None, "Fallback Actor returned an incomplete product record."

        log.info(
            f"[FALLBACK] Success: "
            f"productId={product.get('productId')}, "
            f"title={product.get('title')!r}"
        )

        return product, None

    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        log.warning(f"[FALLBACK] Failed: {message}")
        return None, message


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
    fallback_enabled: bool = True,
    fallback_actor_id: str = DEFAULT_FALLBACK_ACTOR_ID,
    include_creator_videos: bool = False,
    deep_creator_videos: bool = False,
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

        # Detect TikTok anti-bot / security challenge.
        challenge_markers = (
            "security check",
            "verify to continue",
            "verification",
            "captcha",
        )
        normalized_title = (page_title or "").strip().lower()

        if any(marker in normalized_title for marker in challenge_markers):
            log.warning(
                f"[PRODUCT] TikTok security challenge detected: {url}"
            )

            # Keep diagnostics from our own scraper.
            if debug:
                try:
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
                    log.info(
                        f"[PRODUCT] security-check HTML saved to kv://{key}"
                    )
                except Exception as exc:
                    log.warning(
                        f"[PRODUCT] Could not save security-check HTML: {exc}"
                    )

            if capture_screenshots:
                try:
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
                    log.info(
                        f"[PRODUCT] security-check screenshot saved to kv://{key}"
                    )
                except Exception as exc:
                    log.warning(
                        f"[PRODUCT] Could not save security-check screenshot: {exc}"
                    )

            # Fallback to a dedicated TikTok Shop detail Actor.
            if fallback_enabled:
                product, fallback_error = await call_product_fallback(
                    source_url=url,
                    region=region,
                    include_creator_videos=include_creator_videos,
                    deep_creator_videos=deep_creator_videos,
                    fallback_actor_id=fallback_actor_id,
                    log=log,
                )

                if product is not None:
                    await Actor.push_data(product)
                    return
            else:
                fallback_error = "Fallback disabled by input."

            # If fallback also fails, return a clean blocked record.
            await Actor.push_data(
                {
                    "type": "blocked",
                    "sourceUrl": url,
                    "finalUrl": page.url,
                    "productId": extract_product_id(page.url)
                    or extract_product_id(url),
                    "region": region,
                    "httpStatus": status,
                    "reason": "TIKTOK_SECURITY_CHECK",
                    "title": page_title,
                    "fallbackUsed": bool(fallback_enabled),
                    "fallbackActor": (
                        fallback_actor_id
                        if fallback_enabled
                        else None
                    ),
                    "fallbackError": fallback_error,
                }
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
    fallback_enabled: bool,
    fallback_actor_id: str,
    include_creator_videos: bool,
    deep_creator_videos: bool,
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
                    fallback_enabled,
                    fallback_actor_id,
                    include_creator_videos,
                    deep_creator_videos,
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
# Mobile detail enrichment
# ---------------------

def parse_money_text(value: Any) -> Optional[float]:
    """Extract a numeric amount from values such as '€18,84' or '$59.99'."""
    if value is None:
        return None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)

    raw = str(value).strip()
    if not raw:
        return None

    # Keep digits, decimal separators and minus sign only.
    cleaned = re.sub(r"[^0-9,.\-]", "", raw)

    if not cleaned:
        return None

    # German/European decimal notation.
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    elif "," in cleaned and "." in cleaned:
        # Decide decimal separator by the last separator.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")

    try:
        return float(cleaned)
    except ValueError:
        return None


def find_first_text_by_keys(
    value: Any,
    keys: tuple[str, ...],
) -> Optional[str]:
    """
    Recursively find the first non-empty string under one of the exact keys.
    This is intentionally conservative so we do not invent a description.
    """
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()

        for child in value.values():
            found = find_first_text_by_keys(child, keys)
            if found:
                return found

    elif isinstance(value, list):
        for child in value:
            found = find_first_text_by_keys(child, keys)
            if found:
                return found

    return None


def extract_mobile_detail_product(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Extract the first product from the mobile Actor's full_readable output.
    The Actor returns a wrapper with product_info.products.
    """
    product_info = raw.get("product_info")

    if not isinstance(product_info, dict):
        return None

    products = product_info.get("products")

    if not isinstance(products, list) or not products:
        return None

    first = products[0]

    return first if isinstance(first, dict) else None


def merge_mobile_detail_into_product(
    product: Dict[str, Any],
    raw: Dict[str, Any],
    detail_actor_id: str,
) -> None:
    """Merge verified detail fields into the existing search result."""
    detail = extract_mobile_detail_product(raw)

    if detail is None:
        product["detailStatus"] = "NO_PRODUCT_IN_RESPONSE"
        return

    product["detailStatus"] = "AVAILABLE"
    product["detailSource"] = detail_actor_id

    if detail.get("product_id") is not None:
        product["productId"] = str(detail.get("product_id"))

    if detail.get("title"):
        product["title"] = detail.get("title")

    category = detail.get("category")
    if isinstance(category, dict):
        if category.get("name"):
            product["categoryName"] = category.get("name")
        if category.get("id") is not None:
            product["categoryId"] = str(category.get("id"))

    pricing = detail.get("pricing")
    if isinstance(pricing, dict):
        sale_price_text = pricing.get("sale_price")
        original_price_text = pricing.get("original_price")
        currency = pricing.get("currency")

        product["priceText"] = sale_price_text
        product["originalPriceText"] = original_price_text

        parsed_sale = parse_money_text(sale_price_text)
        parsed_original = parse_money_text(original_price_text)

        if parsed_sale is not None:
            # Replace ambiguous search-provider price with detail price.
            product["price"] = parsed_sale
            product["priceSource"] = detail_actor_id

        if parsed_original is not None:
            product["originalPrice"] = parsed_original

        if currency:
            product["currency"] = currency

        if (
            parsed_sale is not None
            and parsed_original is not None
            and parsed_original > 0
            and parsed_original > parsed_sale
        ):
            product["discountPercent"] = round(
                ((parsed_original - parsed_sale) / parsed_original) * 100,
                2,
            )

    sales = detail.get("sales")
    if isinstance(sales, dict) and sales.get("sold_count") is not None:
        product["soldCount"] = sales.get("sold_count")

    inventory = detail.get("inventory")
    if isinstance(inventory, dict):
        if inventory.get("total_stock") is not None:
            product["totalStock"] = inventory.get("total_stock")

        skus = inventory.get("skus")
        if isinstance(skus, list):
            product["skus"] = skus

    media = detail.get("media")
    if isinstance(media, dict):
        image_urls = media.get("image_urls")
        if isinstance(image_urls, list) and image_urls:
            product["images"] = image_urls
            product["image"] = image_urls[0]

    seller = detail.get("seller")
    if isinstance(seller, dict):
        if seller.get("name"):
            product["shopName"] = seller.get("name")
        if seller.get("seller_id") is not None:
            product["sellerId"] = str(seller.get("seller_id"))
        if seller.get("rating") is not None:
            product["shopRating"] = seller.get("rating")
        if seller.get("location"):
            product["sellerLocation"] = seller.get("location")

    # Best-effort product description from any explicit description field
    # exposed in the readable/raw product payload. If none exists, leave null.
    description = find_first_text_by_keys(
        detail,
        (
            "description",
            "product_description",
            "productDescription",
            "description_text",
            "desc",
        ),
    )

    if description:
        product["description"] = description
        product["descriptionStatus"] = "AVAILABLE_MOBILE_API"
    elif product.get("description"):
        product["descriptionStatus"] = "AVAILABLE_SEARCH_PROVIDER"
    else:
        product["descriptionStatus"] = "NOT_AVAILABLE"


async def enrich_products_with_mobile_details(
    products: List[Dict[str, Any]],
    max_products: int,
    region: str,
    detail_actor_id: str,
    log,
) -> None:
    """
    Enrich up to max_products using a mobile TikTok Shop product API Actor.
    This avoids browser CAPTCHA / Security Check pages.
    """
    if max_products <= 0:
        return

    candidates = [
        product
        for product in products
        if product.get("productId") or product.get("productUrl")
    ][:max_products]

    if not candidates:
        log.info("[DETAIL] No products available for mobile detail enrichment.")
        return

    log.info(
        f"[DETAIL] Enriching {len(candidates)} products with {detail_actor_id}."
    )

    for index, product in enumerate(candidates, start=1):
        product_input = (
            str(product.get("productId"))
            if product.get("productId")
            else str(product.get("productUrl"))
        )

        run_input = {
            "productInput": product_input,
            "region": region.upper(),
            "outputMode": "full_readable",
        }

        try:
            log.info(
                f"[DETAIL] ({index}/{len(candidates)}) "
                f"product={product_input}, region={region.upper()}"
            )

            run = await Actor.call(
                actor_id=detail_actor_id,
                run_input=run_input,
            )

            if run is None:
                product["detailStatus"] = "ERROR"
                product["detailError"] = "Detail Actor failed to start."
                continue

            run_client = Actor.apify_client.run(run.id)
            dataset_client = run_client.dataset()
            page = await dataset_client.list_items(limit=1)
            items = page.items or []

            if not items or not isinstance(items[0], dict):
                product["detailStatus"] = "NO_RESULT"
                product["detailError"] = "Detail Actor returned no dataset item."
                continue

            merge_mobile_detail_into_product(
                product=product,
                raw=items[0],
                detail_actor_id=detail_actor_id,
            )

            log.info(
                f"[DETAIL] ({index}/{len(candidates)}) "
                f"status={product.get('detailStatus')}, "
                f"title={product.get('title')!r}"
            )

        except Exception as exc:
            product["detailStatus"] = "ERROR"
            product["detailError"] = f"{type(exc).__name__}: {exc}"
            log.warning(
                f"[DETAIL] Failed for {product_input}: {product['detailError']}"
            )


# ---------------------
# Search result screenshot enrichment
# ---------------------

async def enrich_search_products_with_screenshots(
    products: List[Dict[str, Any]],
    max_screenshots: int,
    proxy_configuration,
    accept_language: str,
    timeouts: Dict[str, Any],
    kv_store: KeyValueStore,
    log,
    debug: bool = False,
) -> None:
    """
    Attempt full-page screenshots for up to five search results.

    Important:
    - If TikTok returns a Security Check / CAPTCHA, we DO NOT store the blank
      screenshot as a successful product screenshot.
    - The result is marked SECURITY_CHECK and keeps the product image URL from
      the search/mobile data as fallback visual evidence.
    """
    max_screenshots = max(0, min(5, int(max_screenshots)))

    if max_screenshots <= 0:
        return

    candidates = [
        product
        for product in products
        if product.get("productUrl")
    ][:max_screenshots]

    if not candidates:
        log.info("[SCREENSHOT] No product URLs available.")
        return

    log.info(
        f"[SCREENSHOT] Attempting {len(candidates)} full-page product screenshots."
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        try:
            for index, product in enumerate(candidates, start=1):
                url = str(product.get("productUrl") or "").strip()

                if not url:
                    continue

                product["fallbackVisualUrl"] = (
                    product.get("image")
                    or (
                        product.get("images")[0]
                        if isinstance(product.get("images"), list)
                        and product.get("images")
                        else None
                    )
                )

                proxy_url: Optional[str] = None

                if proxy_configuration is not None:
                    try:
                        proxy_url = await proxy_configuration.new_url()
                    except Exception as exc:
                        log.warning(
                            f"[SCREENSHOT] Proxy URL failed for {url}: {exc}"
                        )

                context: Optional[BrowserContext] = None
                page: Optional[Page] = None
                response: Optional[Response] = None

                try:
                    context = await create_context_for_req(
                        browser=browser,
                        accept_language=accept_language,
                        proxy_url=proxy_url,
                        use_mobile=True,
                    )

                    page = await context.new_page()

                    log.info(
                        f"[SCREENSHOT] ({index}/{len(candidates)}) Opening {url}"
                    )

                    response = await page.goto(
                        url,
                        timeout=timeouts.get(
                            "navigation",
                            DEFAULT_NAVIGATION_TIMEOUT_MS,
                        ),
                        wait_until="domcontentloaded",
                    )

                    await page.wait_for_timeout(2_000)

                    try:
                        page_title = await page.title()
                    except Exception:
                        page_title = None

                    product["pageTitle"] = page_title
                    product["screenshotHttpStatus"] = (
                        response.status
                        if response is not None
                        else None
                    )
                    product["screenshotFinalUrl"] = page.url

                    normalized_title = (page_title or "").strip().lower()

                    challenge_markers = (
                        "security check",
                        "verify to continue",
                        "verification",
                        "captcha",
                    )

                    blocked = any(
                        marker in normalized_title
                        for marker in challenge_markers
                    )

                    if blocked:
                        product["screenshotStatus"] = "SECURITY_CHECK"
                        product["screenshotKey"] = None

                        # Do not overwrite a good description collected from
                        # the mobile detail Actor.
                        if not product.get("description"):
                            product["descriptionStatus"] = (
                                "NOT_AVAILABLE_SECURITY_CHECK"
                            )

                        log.warning(
                            f"[SCREENSHOT] Security Check for {url}; "
                            f"blank screenshot was NOT stored."
                        )

                        if debug:
                            try:
                                html_key = stable_key(
                                    "security-check-debug",
                                    str(product.get("productId") or url),
                                    "html",
                                )

                                await kv_store.set_value(
                                    html_key,
                                    await page.content(),
                                    content_type="text/html",
                                )

                                product["debugHtmlKey"] = html_key
                            except Exception as exc:
                                log.warning(
                                    f"[SCREENSHOT] Security debug HTML failed: {exc}"
                                )

                        continue

                    # The real product page loaded. Try to extract description
                    # directly from the page without inventing anything.
                    page_description = (
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
                        raw_json_ld = await page.locator(
                            'script[type="application/ld+json"]'
                        ).all_text_contents()
                    except Exception:
                        raw_json_ld = []

                    structured = parse_json_ld_product(raw_json_ld)

                    if not product.get("description"):
                        product["description"] = (
                            structured.get("description")
                            or page_description
                        )

                    if product.get("description"):
                        product["descriptionStatus"] = "AVAILABLE_PRODUCT_PAGE"
                    elif not product.get("descriptionStatus"):
                        product["descriptionStatus"] = "NOT_FOUND"

                    screenshot_key = stable_key(
                        "search-product",
                        str(product.get("productId") or url),
                        "png",
                    )

                    screenshot = await page.screenshot(full_page=True)

                    await kv_store.set_value(
                        screenshot_key,
                        screenshot,
                        content_type="image/png",
                    )

                    product["screenshotKey"] = screenshot_key
                    product["screenshotStatus"] = "PRODUCT_PAGE"

                    log.info(
                        f"[SCREENSHOT] Saved real product page kv://{screenshot_key}"
                    )

                    if debug:
                        try:
                            html_key = stable_key(
                                "search-product-debug",
                                str(product.get("productId") or url),
                                "html",
                            )

                            await kv_store.set_value(
                                html_key,
                                await page.content(),
                                content_type="text/html",
                            )

                            product["debugHtmlKey"] = html_key
                        except Exception as exc:
                            log.warning(
                                f"[SCREENSHOT] Debug HTML failed for {url}: {exc}"
                            )

                except Exception as exc:
                    product["screenshotStatus"] = "ERROR"
                    product["screenshotKey"] = None
                    product["screenshotError"] = (
                        f"{type(exc).__name__}: {exc}"
                    )

                    log.warning(
                        f"[SCREENSHOT] Failed for {url}: "
                        f"{product['screenshotError']}"
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

        finally:
            await browser.close()


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

        mode = str(
            input_data.get("mode", "auto")
        ).strip().lower()

        if mode not in {"auto", "search", "product"}:
            raise ValueError(
                "mode must be one of: auto, search, product"
            )

        region = str(
            input_data.get("region", "DE")
        ).strip().upper() or "DE"

        # ---------------------
        # SEARCH MODE
        # ---------------------

        keywords = (
            input_data.get("keywords")
            or input_data.get("keyword")
            or input_data.get("searchKeywords")
            or []
        )

        if isinstance(keywords, str):
            keywords = [keywords]

        tiktok_categories = (
            input_data.get("tiktokCategories")
            or []
        )

        search_queries: List[str] = []

        for keyword in keywords:
            value = str(keyword).strip()
            if value:
                search_queries.append(value)

        # Category labels become broad search terms for the search provider.
        category_query_map = {
            "beauty": "Beauty",
            "fashion": "Fashion",
            "electronics": "Elektronik",
            "home": "Haushalt",
            "pets": "Tierbedarf",
            "sports": "Sport",
            "food": "Lebensmittel",
            "baby": "Baby",
            "health": "Gesundheit",
        }

        for category in tiktok_categories:
            raw_category = str(category).strip()
            if not raw_category:
                continue

            search_queries.append(
                category_query_map.get(
                    raw_category.lower(),
                    raw_category,
                )
            )

        # Deduplicate while preserving order.
        search_queries = list(dict.fromkeys(search_queries))

        run_search = (
            mode in {"auto", "search"}
            and bool(search_queries)
        )

        search_actor_id = str(
            input_data.get(
                "searchActorId",
                DEFAULT_SEARCH_ACTOR_ID,
            )
        ).strip() or DEFAULT_SEARCH_ACTOR_ID

        max_items = max(
            1,
            int(input_data.get("maxItems", 100)),
        )

        only_discounted = bool(
            input_data.get("onlyDiscounted", False)
        )

        min_discount = float(
            input_data.get("minDiscount", 0) or 0
        )

        min_sold = int(
            input_data.get("minSold", 0) or 0
        )

        min_rating = float(
            input_data.get("minRating", 0) or 0
        )

        sort_by = str(
            input_data.get("sortBy", "discount")
        ).strip().lower()

        search_errors: List[Dict[str, str]] = []
        search_products: List[Dict[str, Any]] = []

        if run_search:
            for query in search_queries:
                products, error = await call_search_provider(
                    query=query,
                    region=region,
                    max_items=max_items,
                    search_actor_id=search_actor_id,
                    log=log,
                )

                search_products.extend(products)

                if error:
                    search_errors.append(
                        {
                            "query": query,
                            "error": error,
                        }
                    )

            # Deduplicate by productId, otherwise URL, otherwise title.
            deduped: Dict[str, Dict[str, Any]] = {}

            for product in search_products:
                identity = (
                    product.get("productId")
                    or product.get("productUrl")
                    or product.get("title")
                )

                if not identity:
                    continue

                deduped[str(identity)] = product

            filtered_products = [
                product
                for product in deduped.values()
                if product_passes_filters(
                    product=product,
                    only_discounted=only_discounted,
                    min_discount=min_discount,
                    min_sold=min_sold,
                    min_rating=min_rating,
                )
            ]

            filtered_products = sort_products(
                filtered_products,
                sort_by=sort_by,
            )

            enrich_search_details = bool(
                input_data.get(
                    "enrichSearchDetails",
                    True,
                )
            )

            max_detail_products = max(
                0,
                min(
                    5,
                    int(
                        input_data.get(
                            "maxDetailProducts",
                            5,
                        )
                    ),
                ),
            )

            detail_actor_id = str(
                input_data.get(
                    "detailActorId",
                    DEFAULT_DETAIL_ACTOR_ID,
                )
            ).strip() or DEFAULT_DETAIL_ACTOR_ID

            if enrich_search_details and filtered_products:
                await enrich_products_with_mobile_details(
                    products=filtered_products,
                    max_products=max_detail_products,
                    region=region,
                    detail_actor_id=detail_actor_id,
                    log=log,
                )

            capture_search_screenshots = bool(
                input_data.get(
                    "captureSearchScreenshots",
                    False,
                )
            )

            max_search_screenshots = max(
                0,
                min(
                    5,
                    int(
                        input_data.get(
                            "maxSearchScreenshots",
                            5,
                        )
                    ),
                ),
            )

            if capture_search_screenshots and filtered_products:
                accept_language_for_search = input_data.get(
                    "acceptLanguage",
                    "de-DE,de;q=0.9,en;q=0.8",
                )

                screenshot_timeouts = ms_timeouts_from_input(
                    input_data.get("timeouts", {})
                )

                screenshot_debug = bool(
                    input_data.get(
                        "debug",
                        False,
                    )
                )

                screenshot_proxy_configuration = (
                    await Actor.create_proxy_configuration(
                        actor_proxy_input=input_data.get(
                            "proxyConfiguration"
                        )
                    )
                )

                screenshot_kv_store = await KeyValueStore.open()

                await enrich_search_products_with_screenshots(
                    products=filtered_products,
                    max_screenshots=max_search_screenshots,
                    proxy_configuration=screenshot_proxy_configuration,
                    accept_language=accept_language_for_search,
                    timeouts=screenshot_timeouts,
                    kv_store=screenshot_kv_store,
                    log=log,
                    debug=screenshot_debug,
                )

            if filtered_products:
                await Actor.push_data(
                    filtered_products
                )

            await Actor.set_value(
                "SEARCH_SUMMARY",
                {
                    "mode": mode,
                    "region": region,
                    "queries": search_queries,
                    "searchActorId": search_actor_id,
                    "productsReceived": len(search_products),
                    "uniqueProducts": len(deduped),
                    "productsSaved": len(filtered_products),
                    "enrichSearchDetails": enrich_search_details,
                    "maxDetailProducts": max_detail_products,
                    "detailActorId": detail_actor_id,
                    "captureSearchScreenshots": capture_search_screenshots,
                    "maxSearchScreenshots": max_search_screenshots,
                    "screenshotsAttempted": (
                        min(
                            max_search_screenshots,
                            len(filtered_products),
                        )
                        if capture_search_screenshots
                        else 0
                    ),
                    "filters": {
                        "onlyDiscounted": only_discounted,
                        "minDiscount": min_discount,
                        "minSold": min_sold,
                        "minRating": min_rating,
                        "sortBy": sort_by,
                    },
                    "errors": search_errors,
                },
            )

            log.info(
                f"[SEARCH] Finished: "
                f"received={len(search_products)}, "
                f"unique={len(deduped)}, "
                f"saved={len(filtered_products)}"
            )

        # In explicit search mode, do not launch Playwright at all.
        if mode == "search":
            return

        # ---------------------
        # PRODUCT / DIRECT MODE
        # ---------------------

        raw_start = (
            input_data.get("startUrls")
            or input_data.get("start_urls")
            or input_data.get("productUrls")
            or []
        )

        start_items = normalize_start_items(
            raw_start
        )

        # Seller and manual category URLs remain best-effort Playwright discovery.
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

        category_urls = (
            input_data.get("categoryUrls")
            or []
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

        # In auto mode, categories/keywords are handled by the search provider,
        # so we deliberately do not turn them into TikTok /tag/ pages.
        if not start_items:
            if run_search:
                log.info(
                    "[AUTO] Search completed and there are no direct "
                    "product/listing URLs to process."
                )
                return

            log.warning(
                "No direct product URLs, seller handles, "
                "or category URLs were provided."
            )
            return

        timeouts = ms_timeouts_from_input(
            input_data.get("timeouts", {})
        )

        accept_language = input_data.get(
            "acceptLanguage",
            "de-DE,de;q=0.9,en;q=0.8",
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

        fallback_enabled = bool(
            input_data.get(
                "fallbackEnabled",
                True,
            )
        )

        fallback_actor_id = str(
            input_data.get(
                "fallbackActorId",
                DEFAULT_FALLBACK_ACTOR_ID,
            )
        ).strip() or DEFAULT_FALLBACK_ACTOR_ID

        include_creator_videos = bool(
            input_data.get(
                "includeCreatorVideos",
                False,
            )
        )

        deep_creator_videos = bool(
            input_data.get(
                "deepCreatorVideos",
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
            f"Added {added} direct requests."
        )

        if added == 0:
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
                    fallback_enabled=fallback_enabled,
                    fallback_actor_id=fallback_actor_id,
                    include_creator_videos=include_creator_videos,
                    deep_creator_videos=deep_creator_videos,
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
