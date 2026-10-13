#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from apify import Actor


VERSION = "6.0-advanced"
DEFAULT_SEARCH_ACTOR_ID = "getanyapi/tiktok-shop-scraper"
DEFAULT_MARKET_ACTOR_ID = "lergassy/tiktok-shop-top-products-scraper"
DEFAULT_CREATORS_ACTOR_ID = "lergassy/tiktok-shop-creators-scraper"
DEFAULT_VIDEOS_ACTOR_ID = "lergassy/tiktok-shop-videos-scraper"
DEFAULT_HISTORY_STORE = "tiktok-shop-top-products-v6-history"

SUPPORTED_MARKET_INTEL_REGIONS = {
    "US", "GB", "ID", "TH", "VN", "PH", "MY", "SG",
    "JP", "MX", "DE", "IT", "FR", "ES", "BR",
}

CATEGORY_QUERY_MAP = {
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

CURRENCY_BY_REGION = {
    "DE": "EUR",
    "GB": "GBP",
    "US": "USD",
    "SG": "SGD",
    "MY": "MYR",
    "TH": "THB",
    "VN": "VND",
    "PH": "PHP",
    "ID": "IDR",
    "JP": "JPY",
    "MX": "MXN",
    "IT": "EUR",
    "FR": "EUR",
    "ES": "EUR",
    "BR": "BRL",
}


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def as_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except (TypeError, ValueError):
            return None

    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    text = (
        text.replace("%", "")
        .replace("€", "")
        .replace("$", "")
        .replace("£", "")
        .replace("\u00a0", "")
        .replace(" ", "")
    )

    # European notation: 1.234,56 -> 1234.56
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")

    text = re.sub(r"[^0-9.\-]", "", text)

    if not text or text in {"-", ".", "-."}:
        return None

    try:
        result = float(text)
        return result if math.isfinite(result) else None
    except ValueError:
        return None


def as_int(value: Any) -> Optional[int]:
    number = as_float(value)
    if number is None:
        return None
    return int(round(number))


def as_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return bool(value)

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "in_stock", "available"}:
            return True
        if normalized in {"false", "0", "no", "n", "out_of_stock", "unavailable"}:
            return False

    return None


def safe_percent_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous is None or previous == 0:
        return None
    return round(((current - previous) / previous) * 100.0, 2)


def safe_delta(
    current: Optional[float],
    previous: Optional[float],
    *,
    counter: bool = False,
) -> Optional[float]:
    if current is None or previous is None:
        return None

    delta = current - previous

    if counter and delta < 0:
        # Cumulative counters should normally never decrease. Treat a decrease
        # as a provider reset/data anomaly instead of producing negative sales.
        return None

    return round(delta, 4)


def round_or_none(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    return round(value, digits)


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def sanitize_store_name(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")

    if not value:
        value = DEFAULT_HISTORY_STORE

    # Keep a conservative length.
    return value[:63].strip("-") or DEFAULT_HISTORY_STORE


def parse_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    if isinstance(value, (int, float)):
        number = float(value)
        # Milliseconds epoch.
        if number > 10_000_000_000:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None

    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    if text.isdigit():
        return parse_datetime(int(text))

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def iso_or_none(value: Any) -> Optional[str]:
    dt = parse_datetime(value)
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def recursive_find_with_key(
    value: Any,
    aliases: Iterable[str],
) -> Tuple[Optional[str], Any]:
    """
    Find the first exact-key match, checking the current dict before descendants.
    Returns (matched_key, value).
    """
    alias_set = set(aliases)

    if isinstance(value, dict):
        for key in alias_set:
            if key in value and value[key] is not None:
                return key, value[key]

        for child in value.values():
            found_key, found_value = recursive_find_with_key(child, alias_set)
            if found_key is not None:
                return found_key, found_value

    elif isinstance(value, list):
        for child in value:
            found_key, found_value = recursive_find_with_key(child, alias_set)
            if found_key is not None:
                return found_key, found_value

    return None, None


def recursive_find_number(
    value: Any,
    aliases: Iterable[str],
) -> Tuple[Optional[str], Optional[float]]:
    key, raw = recursive_find_with_key(value, aliases)
    return key, as_float(raw)


def recursive_find_int(
    value: Any,
    aliases: Iterable[str],
) -> Tuple[Optional[str], Optional[int]]:
    key, raw = recursive_find_with_key(value, aliases)
    return key, as_int(raw)


def recursive_find_bool(
    value: Any,
    aliases: Iterable[str],
) -> Tuple[Optional[str], Optional[bool]]:
    key, raw = recursive_find_with_key(value, aliases)
    return key, as_bool(raw)


def product_id_from_any(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{16,22}", text):
        return text
    match = re.search(r"(?<!\d)(\d{16,22})(?!\d)", text)
    return match.group(1) if match else None


def add_source(product: Dict[str, Any], source: str) -> None:
    sources = list(product.get("dataSources") or [])
    if source and source not in sources:
        sources.append(source)
    product["dataSources"] = sources


def merge_if_missing(product: Dict[str, Any], field: str, value: Any) -> None:
    if product.get(field) is None and value is not None:
        product[field] = value


# ---------------------------------------------------------------------------
# Provider normalization
# ---------------------------------------------------------------------------

def normalize_price(
    raw_value: Any,
    *,
    mode: str,
    divisor: float,
    auto_minor_threshold: float,
) -> Optional[float]:
    value = as_float(raw_value)

    if value is None:
        return None

    if mode == "raw":
        return round(value, 2)

    if mode == "minor_units":
        if divisor <= 0:
            raise ValueError("priceDivisor must be greater than 0.")
        return round(value / divisor, 2)

    # auto:
    # - numbers containing decimals are treated as already-normalized currency
    # - integer-like values >= threshold are interpreted as minor units
    # - small integers are kept as major units
    integer_like = abs(value - round(value)) < 1e-9

    if integer_like and abs(value) >= auto_minor_threshold:
        if divisor <= 0:
            raise ValueError("priceDivisor must be greater than 0.")
        return round(value / divisor, 2)

    return round(value, 2)


def normalize_commission_percent(raw: Dict[str, Any]) -> Tuple[Optional[float], Any, Optional[str]]:
    key, raw_value = recursive_find_with_key(
        raw,
        (
            "commissionPercent",
            "commission_percent",
            "commissionRate",
            "commission_rate",
            "affiliateCommissionRate",
            "affiliate_commission_rate",
            "commission",
        ),
    )

    value = as_float(raw_value)

    if value is None:
        return None, raw_value, key

    # Fractional rates such as 0.15 usually mean 15%.
    if key and "rate" in key.lower() and 0 <= value <= 1:
        value *= 100.0

    if not 0 <= value <= 100:
        return None, raw_value, key

    return round(value, 2), raw_value, key


def normalize_product(
    raw: Dict[str, Any],
    *,
    query: str,
    region: str,
    provider_actor_id: str,
    price_mode: str,
    price_divisor: float,
    auto_minor_threshold: float,
    include_raw_provider_data: bool,
) -> Dict[str, Any]:
    price_raw = raw.get("price")
    original_price_raw = raw.get("originalPrice")

    current_price = normalize_price(
        price_raw,
        mode=price_mode,
        divisor=price_divisor,
        auto_minor_threshold=auto_minor_threshold,
    )
    original_price = normalize_price(
        original_price_raw,
        mode=price_mode,
        divisor=price_divisor,
        auto_minor_threshold=auto_minor_threshold,
    )

    raw_discount = as_float(
        raw.get("discountPercent")
        if raw.get("discountPercent") is not None
        else raw.get("discount")
    )

    if (
        current_price is not None
        and original_price is not None
        and original_price > current_price
        and original_price > 0
    ):
        discount_percent = round(
            ((original_price - current_price) / original_price) * 100.0,
            2,
        )
    elif raw_discount is not None and 0 <= raw_discount <= 100:
        discount_percent = round(raw_discount, 2)
    else:
        discount_percent = None

    stock_key, stock = recursive_find_int(
        raw,
        (
            "availableQuantity",
            "available_quantity",
            "stock",
            "stockCount",
            "stock_count",
            "totalStock",
            "total_stock",
            "inventoryCount",
            "inventory_count",
            "quantityAvailable",
            "quantity_available",
        ),
    )

    in_stock_key, in_stock = recursive_find_bool(
        raw,
        (
            "inStock",
            "in_stock",
            "isInStock",
            "is_in_stock",
            "available",
            "isAvailable",
            "is_available",
        ),
    )

    if in_stock is None and stock is not None:
        in_stock = stock > 0

    creator_key, creator_count = recursive_find_int(
        raw,
        (
            "creatorCount",
            "creator_count",
            "affiliateCreatorCount",
            "affiliate_creator_count",
            "creatorsCount",
            "creators_count",
            "creator_num",
        ),
    )

    video_key, video_count = recursive_find_int(
        raw,
        (
            "videoCount",
            "video_count",
            "creatorVideoCount",
            "creator_video_count",
            "creatorVideosCount",
            "creator_videos_count",
        ),
    )

    views_key, views = recursive_find_int(
        raw,
        (
            "views",
            "viewCount",
            "view_count",
            "videoViews",
            "video_views",
            "totalViews",
            "total_views",
        ),
    )

    likes_key, likes = recursive_find_int(
        raw,
        ("likes", "likeCount", "like_count", "totalLikes", "total_likes"),
    )
    comments_key, comments = recursive_find_int(
        raw,
        ("comments", "commentCount", "comment_count", "totalComments", "total_comments"),
    )
    shares_key, shares = recursive_find_int(
        raw,
        ("shares", "shareCount", "share_count", "totalShares", "total_shares"),
    )

    engagement_rate = None
    if views is not None and views > 0:
        engagement_total = sum(
            value or 0
            for value in (likes, comments, shares)
        )
        engagement_rate = round((engagement_total / views) * 100.0, 4)

    commission_percent, commission_raw, commission_key = normalize_commission_percent(raw)

    listed_key, listed_raw = recursive_find_with_key(
        raw,
        (
            "listedAt",
            "listed_at",
            "createdAt",
            "created_at",
            "createTime",
            "create_time",
            "publishTime",
            "publish_time",
        ),
    )
    listed_at = iso_or_none(listed_raw)

    currency = raw.get("currency")
    currency_inferred = False

    if not currency:
        currency = CURRENCY_BY_REGION.get(region.upper())
        currency_inferred = currency is not None

    product: Dict[str, Any] = {
        "type": "product",
        "source": raw.get("source") or provider_actor_id,
        "dataSources": [raw.get("source") or provider_actor_id],
        "searchQuery": query,
        "searchQueries": [query],
        "region": region.upper(),
        "productId": (str(raw.get("productId")) if raw.get("productId") is not None else product_id_from_any(raw.get("url") or raw.get("productUrl"))),
        "title": raw.get("title"),
        "description": (
            raw.get("description")
            or raw.get("productDescription")
            or raw.get("product_description")
        ),
        "productUrl": raw.get("url") or raw.get("productUrl"),
        "image": raw.get("image"),
        "shopName": raw.get("shopName"),
        "sellerId": str(raw.get("sellerId")) if raw.get("sellerId") is not None else None,

        # Price
        "priceRaw": price_raw,
        "originalPriceRaw": original_price_raw,
        "currentPrice": current_price,
        "originalPrice": original_price,
        "currency": currency,
        "currencyInferred": currency_inferred,
        "discountPercent": discount_percent,
        "priceNormalizationMode": price_mode,

        # Product performance
        "soldCount": as_int(raw.get("soldCount")),
        "rating": as_float(raw.get("rating")),
        "reviewCount": as_int(raw.get("reviewCount")),

        # Creator / content
        "creatorCount": creator_count,
        "videoCount": video_count,
        "views": views,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "engagementRate": engagement_rate,

        # Affiliate
        "commissionPercent": commission_percent,

        # Stock
        "stock": stock,
        "inStock": in_stock,

        # Age
        "listedAt": listed_at,

        # Diagnostics showing which provider key supplied optional fields.
        "providerFieldKeys": {
            "stock": stock_key,
            "inStock": in_stock_key,
            "creatorCount": creator_key,
            "videoCount": video_key,
            "views": views_key,
            "likes": likes_key,
            "comments": comments_key,
            "shares": shares_key,
            "commission": commission_key,
            "listedAt": listed_key,
        },
        "commissionRaw": commission_raw,
    }

    if include_raw_provider_data:
        product["rawProviderData"] = raw

    return product


# ---------------------------------------------------------------------------
# Search provider
# ---------------------------------------------------------------------------

async def call_search_provider(
    *,
    query: str,
    region: str,
    max_items: int,
    search_actor_id: str,
    price_mode: str,
    price_divisor: float,
    auto_minor_threshold: float,
    include_raw_provider_data: bool,
    log,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    run_input = {
        "query": query,
        "country": region.upper(),
        "maxItems": max_items,
    }

    try:
        log.info(
            f"[SEARCH] Actor={search_actor_id} query={query!r} "
            f"country={region.upper()} maxItems={max_items}"
        )

        run = await Actor.call(
            actor_id=search_actor_id,
            run_input=run_input,
        )

        if run is None:
            return [], "Search Actor failed to start."

        run_client = Actor.apify_client.run(run.id)
        dataset_client = run_client.dataset()
        page = await dataset_client.list_items(limit=max_items)
        raw_items = page.items or []

        products: List[Dict[str, Any]] = []

        for raw in raw_items:
            if not isinstance(raw, dict):
                continue

            products.append(
                normalize_product(
                    raw,
                    query=query,
                    region=region,
                    provider_actor_id=search_actor_id,
                    price_mode=price_mode,
                    price_divisor=price_divisor,
                    auto_minor_threshold=auto_minor_threshold,
                    include_raw_provider_data=include_raw_provider_data,
                )
            )

        log.info(f"[SEARCH] {query!r}: {len(products)} products")
        return products, None

    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        log.warning(f"[SEARCH] {query!r} failed: {message}")
        return [], message


def deduplicate_products(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: Dict[str, Dict[str, Any]] = {}

    for product in products:
        identity = (
            product.get("productId")
            or product.get("productUrl")
            or product.get("title")
        )

        if not identity:
            continue

        identity = str(identity)

        if identity not in deduped:
            deduped[identity] = product
            continue

        existing = deduped[identity]
        queries = list(existing.get("searchQueries") or [])

        for query in product.get("searchQueries") or []:
            if query not in queries:
                queries.append(query)

        existing["searchQueries"] = queries

    return list(deduped.values())


# ---------------------------------------------------------------------------
# Advanced multi-source market / creator / video intelligence
# ---------------------------------------------------------------------------

async def read_run_dataset(run: Any, *, limit: int) -> List[Dict[str, Any]]:
    if run is None:
        return []
    run_client = Actor.apify_client.run(run.id)
    dataset_client = run_client.dataset()
    page = await dataset_client.list_items(limit=max(1, limit))
    return [item for item in (page.items or []) if isinstance(item, dict)]


async def call_market_intelligence(
    *,
    actor_id: str,
    keywords: Sequence[str],
    categories: Sequence[str],
    region: str,
    period: str,
    max_results_per_source: int,
    add_analytics: bool,
    overall_top: bool,
    log,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    if region not in SUPPORTED_MARKET_INTEL_REGIONS:
        return [], f"Market intelligence source does not support region {region}."

    run_input: Dict[str, Any] = {
        "country": region,
        "period": period,
        "sortBy": "unitsSold",
        "maxResultsPerSource": max_results_per_source,
        "addAnalytics": add_analytics,
    }
    if not overall_top:
        if keywords:
            run_input["keywords"] = list(keywords)
        if categories:
            run_input["categories"] = list(categories)

    try:
        log.info(
            f"[MARKET] actor={actor_id} region={region} period={period} "
            f"overall={overall_top} keywords={len(keywords)} categories={len(categories)}"
        )
        run = await Actor.call(actor_id=actor_id, run_input=run_input)
        sources = 1 if overall_top else max(1, len(keywords) + len(categories))
        rows = await read_run_dataset(
            run,
            limit=max_results_per_source * sources + 50,
        )
        rows = [
            row for row in rows
            if str(row.get("type", "product")).lower() == "product"
            and product_id_from_any(row.get("productId") or row.get("url"))
        ]
        log.info(f"[MARKET] {period}: {len(rows)} product rows")
        return rows, None
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        log.warning(f"[MARKET] {period} failed: {message}")
        return [], message


async def call_creators_enrichment(
    *,
    actor_id: str,
    product_inputs: Sequence[str],
    region: str,
    max_results_per_product: int,
    log,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    if not product_inputs:
        return [], None
    try:
        log.info(
            f"[CREATORS] actor={actor_id} products={len(product_inputs)} region={region}"
        )
        run = await Actor.call(
            actor_id=actor_id,
            run_input={
                "productIds": list(product_inputs),
                "country": region,
                "period": "last7Day",
                "sortBy": "revenue",
                "maxResultsPerSource": max_results_per_product,
                "addAnalytics": False,
            },
        )
        rows = await read_run_dataset(
            run,
            limit=len(product_inputs) * max_results_per_product + 50,
        )
        rows = [
            row for row in rows
            if str(row.get("type", "creator")).lower() == "creator"
        ]
        log.info(f"[CREATORS] {len(rows)} creator rows")
        return rows, None
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        log.warning(f"[CREATORS] failed: {message}")
        return [], message


async def call_videos_enrichment(
    *,
    actor_id: str,
    product_inputs: Sequence[str],
    region: str,
    max_results_per_product: int,
    log,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    if not product_inputs:
        return [], None
    try:
        log.info(
            f"[VIDEOS] actor={actor_id} products={len(product_inputs)} region={region}"
        )
        run = await Actor.call(
            actor_id=actor_id,
            run_input={
                "productIds": list(product_inputs),
                "country": region,
                "period": "last7Day",
                "sortBy": "revenue",
                "maxResultsPerSource": max_results_per_product,
                "aiVideos": "all",
                "addAnalytics": False,
            },
        )
        rows = await read_run_dataset(
            run,
            limit=len(product_inputs) * max_results_per_product + 50,
        )
        rows = [
            row for row in rows
            if str(row.get("type", "video")).lower() == "video"
        ]
        log.info(f"[VIDEOS] {len(rows)} video rows")
        return rows, None
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        log.warning(f"[VIDEOS] failed: {message}")
        return [], message


def create_product_from_market_row(
    row: Dict[str, Any],
    *,
    region: str,
    actor_id: str,
) -> Dict[str, Any]:
    product_id = product_id_from_any(row.get("productId") or row.get("url"))
    return {
        "type": "product",
        "source": actor_id,
        "dataSources": [actor_id],
        "searchQuery": None,
        "searchQueries": [],
        "region": region,
        "productId": product_id,
        "title": row.get("title"),
        "description": None,
        "productUrl": row.get("url"),
        "image": row.get("image"),
        "shopName": row.get("shopName"),
        "sellerId": str(row.get("shopId")) if row.get("shopId") is not None else None,
        "priceRaw": None,
        "originalPriceRaw": None,
        "currentPrice": None,
        "originalPrice": None,
        "currency": CURRENCY_BY_REGION.get(region),
        "currencyInferred": True,
        "discountPercent": None,
        "priceNormalizationMode": "market_intel_usd_only",
        "soldCount": None,
        "rating": None,
        "reviewCount": as_int(row.get("reviewCount")),
        "creatorCount": as_int(row.get("creatorCount")),
        "videoCount": as_int(row.get("videoCount")),
        "views": None,
        "likes": None,
        "comments": None,
        "shares": None,
        "engagementRate": None,
        "commissionPercent": as_float(row.get("commissionRatePercent")),
        "stock": None,
        "inStock": None,
        "listedAt": iso_or_none(row.get("launchDate")),
        "providerFieldKeys": {},
        "commissionRaw": row.get("commissionRatePercent"),
    }


def merge_market_row(
    product: Dict[str, Any],
    row: Dict[str, Any],
    *,
    period: str,
    actor_id: str,
) -> None:
    add_source(product, actor_id)
    merge_if_missing(product, "title", row.get("title"))
    merge_if_missing(product, "productUrl", row.get("url"))
    merge_if_missing(product, "listedAt", iso_or_none(row.get("launchDate")))

    commission = as_float(row.get("commissionRatePercent"))
    if commission is not None and 0 <= commission <= 100:
        product["commissionPercent"] = round(commission, 2)
        product["commissionSource"] = f"{actor_id}:{period}"

    for field, value in {
        "creatorCount": as_int(row.get("creatorCount")),
        "videoCount": as_int(row.get("videoCount")),
        "liveCount": as_int(row.get("liveCount")),
        "reviewCount": as_int(row.get("reviewCount")),
        "marketMinPriceUSD": as_float(row.get("minPrice")),
        "marketMaxPriceUSD": as_float(row.get("maxPrice")),
        "marketShopId": str(row.get("shopId")) if row.get("shopId") is not None else None,
        "categoryIds": row.get("categoryIds"),
        "deliveryType": row.get("deliveryType"),
    }.items():
        if value is not None:
            product[field] = value
            if field in {"creatorCount", "videoCount", "reviewCount"}:
                product[f"{field}Source"] = f"{actor_id}:{period}"

    units = as_int(row.get("unitsSold"))
    revenue = as_float(row.get("revenue"))
    avg_price = as_float(row.get("avgSellingPrice"))
    growth = as_float(row.get("revenueGrowthPercent"))
    video_revenue = as_float(row.get("videoRevenue"))
    live_revenue = as_float(row.get("liveRevenue"))
    shop_window_revenue = as_float(row.get("shopWindowRevenue"))

    if period == "lastDay":
        product["marketSold24h"] = units
        product["marketRevenueUSD24h"] = revenue
        product["marketAvgSellingPriceUSD24h"] = avg_price
        product["marketRevenueGrowthPercent24h"] = growth
        product["marketVideoRevenueUSD24h"] = video_revenue
        product["marketLiveRevenueUSD24h"] = live_revenue
        product["marketShopWindowRevenueUSD24h"] = shop_window_revenue
    elif period == "last7Day":
        product["marketSold7d"] = units
        product["marketRevenueUSD7d"] = revenue
        product["marketAvgSellingPriceUSD7d"] = avg_price
        product["marketRevenueGrowthPercent7d"] = growth
        product["marketVideoRevenueUSD7d"] = video_revenue
        product["marketLiveRevenueUSD7d"] = live_revenue
        product["marketShopWindowRevenueUSD7d"] = shop_window_revenue
    product[f"marketRank_{period}"] = as_int(row.get("rank"))


def merge_market_rows_into_products(
    products: List[Dict[str, Any]],
    *,
    rows: Sequence[Dict[str, Any]],
    period: str,
    actor_id: str,
    region: str,
    include_market_only_products: bool,
) -> List[Dict[str, Any]]:
    by_id = {
        str(product["productId"]): product
        for product in products
        if product.get("productId")
    }
    for row in rows:
        product_id = product_id_from_any(row.get("productId") or row.get("url"))
        if not product_id:
            continue
        product = by_id.get(product_id)
        if product is None and include_market_only_products:
            product = create_product_from_market_row(
                row,
                region=region,
                actor_id=actor_id,
            )
            products.append(product)
            by_id[product_id] = product
        if product is not None:
            merge_market_row(product, row, period=period, actor_id=actor_id)
    return products


def product_id_from_source_row(row: Dict[str, Any]) -> Optional[str]:
    for key in ("productId", "sourceInput", "productUrl", "sourceUrl"):
        product_id = product_id_from_any(row.get(key))
        if product_id:
            return product_id
    return None


def aggregate_creators(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for row in rows:
        product_id = product_id_from_source_row(row)
        creator_id = row.get("creatorId") or row.get("username")
        if not product_id or not creator_id:
            continue
        grouped.setdefault(product_id, {})[str(creator_id)] = row

    output: Dict[str, Dict[str, Any]] = {}
    for product_id, creator_map in grouped.items():
        creator_rows = list(creator_map.values())
        top = max(creator_rows, key=lambda r: as_float(r.get("revenue")) or 0.0)
        output[product_id] = {
            "creatorCountObserved7d": len(creator_rows),
            "creatorRevenueUSD7d": round(sum(as_float(r.get("revenue")) or 0 for r in creator_rows), 2),
            "creatorUnitsSold7d": sum(as_int(r.get("unitsSold")) or 0 for r in creator_rows),
            "creatorContentViews7d": sum(as_int(r.get("contentViews")) or 0 for r in creator_rows),
            "topCreatorUsername7d": top.get("username"),
            "topCreatorRevenueUSD7d": round_or_none(as_float(top.get("revenue")), 2),
        }
    return output


def aggregate_videos(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for row in rows:
        product_id = product_id_from_source_row(row)
        video_id = row.get("videoId") or row.get("url")
        if not product_id or not video_id:
            continue
        grouped.setdefault(product_id, {})[str(video_id)] = row

    output: Dict[str, Dict[str, Any]] = {}
    for product_id, video_map in grouped.items():
        video_rows = list(video_map.values())
        total_views = sum(as_int(r.get("views")) or 0 for r in video_rows)
        total_likes = sum(as_int(r.get("likes")) or 0 for r in video_rows)
        total_comments = sum(as_int(r.get("comments")) or 0 for r in video_rows)
        total_shares = sum(as_int(r.get("shares")) or 0 for r in video_rows)
        total_revenue = sum(as_float(r.get("revenue")) or 0 for r in video_rows)
        top = max(video_rows, key=lambda r: as_float(r.get("revenue")) or 0.0)
        unique_creators = {
            str(r.get("creatorId") or r.get("creatorUsername"))
            for r in video_rows
            if r.get("creatorId") or r.get("creatorUsername")
        }
        engagement = (
            round(((total_likes + total_comments + total_shares) / total_views) * 100, 4)
            if total_views > 0 else None
        )
        output[product_id] = {
            "videoCountObserved7d": len(video_rows),
            "videoViews7d": total_views,
            "videoLikes7d": total_likes,
            "videoComments7d": total_comments,
            "videoShares7d": total_shares,
            "videoEngagementRate7d": engagement,
            "videoRevenueUSD7d": round(total_revenue, 2),
            "videoCreatorCountObserved7d": len(unique_creators),
            "topVideoUrl7d": top.get("url"),
            "topVideoRevenueUSD7d": round_or_none(as_float(top.get("revenue")), 2),
        }
    return output


def merge_creator_video_aggregates(
    products: Sequence[Dict[str, Any]],
    *,
    creator_aggregates: Dict[str, Dict[str, Any]],
    video_aggregates: Dict[str, Dict[str, Any]],
    creators_actor_id: str,
    videos_actor_id: str,
) -> None:
    for product in products:
        product_id = str(product.get("productId") or "")
        if not product_id:
            continue
        creator_metrics = creator_aggregates.get(product_id)
        if creator_metrics:
            product.update(creator_metrics)
            add_source(product, creators_actor_id)
            if product.get("creatorCount") is None:
                product["creatorCount"] = creator_metrics["creatorCountObserved7d"]
                product["creatorCountSource"] = f"{creators_actor_id}:observed7d"
        video_metrics = video_aggregates.get(product_id)
        if video_metrics:
            product.update(video_metrics)
            add_source(product, videos_actor_id)
            if product.get("videoCount") is None:
                product["videoCount"] = video_metrics["videoCountObserved7d"]
                product["videoCountSource"] = f"{videos_actor_id}:observed7d"
            if product.get("views") is None:
                product["views"] = video_metrics["videoViews7d"]
                product["viewsSource"] = f"{videos_actor_id}:7d"
            product["likes"] = video_metrics["videoLikes7d"]
            product["comments"] = video_metrics["videoComments7d"]
            product["shares"] = video_metrics["videoShares7d"]
            product["engagementRate"] = video_metrics["videoEngagementRate7d"]


def select_enrichment_products(
    products: Sequence[Dict[str, Any]],
    max_products: int,
) -> List[Dict[str, Any]]:
    return sorted(
        products,
        key=lambda p: (
            as_float(p.get("marketSold7d")) if as_float(p.get("marketSold7d")) is not None else -1,
            as_float(p.get("soldCount")) if as_float(p.get("soldCount")) is not None else -1,
        ),
        reverse=True,
    )[:max_products]


# ---------------------------------------------------------------------------
# Persistent history / snapshots
# ---------------------------------------------------------------------------

SNAPSHOT_FIELDS = (
    "soldCount",
    "currentPrice",
    "reviewCount",
    "creatorCount",
    "views",
    "likes",
    "comments",
    "shares",
    "commissionPercent",
    "stock",
    "inStock",
    "rating",
    "discountPercent",
)


def build_snapshot(product: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    snapshot = {"timestamp": now.isoformat()}

    for field in SNAPSHOT_FIELDS:
        snapshot[field] = product.get(field)

    return snapshot


def valid_history_items(raw_history: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_history, list):
        return []

    out: List[Dict[str, Any]] = []

    for item in raw_history:
        if not isinstance(item, dict):
            continue

        dt = parse_datetime(item.get("timestamp"))
        if dt is None:
            continue

        normalized = dict(item)
        normalized["timestamp"] = dt.astimezone(timezone.utc).isoformat()
        out.append(normalized)

    out.sort(
        key=lambda item: parse_datetime(item.get("timestamp"))
        or datetime.min.replace(tzinfo=timezone.utc)
    )
    return out


def nearest_snapshot(
    history: List[Dict[str, Any]],
    target: datetime,
    tolerance_hours: float,
) -> Optional[Dict[str, Any]]:
    best: Optional[Dict[str, Any]] = None
    best_distance: Optional[float] = None

    for item in history:
        dt = parse_datetime(item.get("timestamp"))
        if dt is None:
            continue

        distance = abs((dt - target).total_seconds()) / 3600.0

        if distance <= tolerance_hours and (
            best_distance is None or distance < best_distance
        ):
            best = item
            best_distance = distance

    return best


def latest_snapshot_with_value(
    history: List[Dict[str, Any]],
    field: str,
) -> Optional[Dict[str, Any]]:
    for item in reversed(history):
        if item.get(field) is not None:
            return item
    return None


def calculate_counter_delta(
    current: Any,
    baseline: Optional[Dict[str, Any]],
    field: str,
) -> Optional[float]:
    if baseline is None:
        return None

    return safe_delta(
        as_float(current),
        as_float(baseline.get(field)),
        counter=True,
    )


def calculate_regular_delta(
    current: Any,
    baseline: Optional[Dict[str, Any]],
    field: str,
) -> Optional[float]:
    if baseline is None:
        return None

    return safe_delta(
        as_float(current),
        as_float(baseline.get(field)),
        counter=False,
    )


def percent_growth_from_counts(
    current: Any,
    baseline: Optional[Dict[str, Any]],
    field: str,
) -> Optional[float]:
    if baseline is None:
        return None

    return safe_percent_change(
        as_float(current),
        as_float(baseline.get(field)),
    )


def compute_history_metrics(
    product: Dict[str, Any],
    *,
    history: List[Dict[str, Any]],
    first_seen: datetime,
    now: datetime,
    tolerance_hours: float,
) -> Dict[str, Any]:
    target_24h = now - timedelta(hours=24)
    target_7d = now - timedelta(days=7)
    target_14d = now - timedelta(days=14)

    baseline_24h = nearest_snapshot(history, target_24h, tolerance_hours)
    baseline_7d = nearest_snapshot(history, target_7d, tolerance_hours)
    baseline_14d = nearest_snapshot(history, target_14d, tolerance_hours)

    sold24h = calculate_counter_delta(product.get("soldCount"), baseline_24h, "soldCount")
    sold7d = calculate_counter_delta(product.get("soldCount"), baseline_7d, "soldCount")

    previous_7d_sales = None

    if baseline_7d is not None and baseline_14d is not None:
        previous_7d_sales = safe_delta(
            as_float(baseline_7d.get("soldCount")),
            as_float(baseline_14d.get("soldCount")),
            counter=True,
        )

    sales_growth_7d = safe_percent_change(sold7d, previous_7d_sales)

    avg_daily_sales_7d = None
    if sold7d is not None:
        avg_daily_sales_7d = round(sold7d / 7.0, 2)

    sales_velocity_ratio = None
    if (
        sold24h is not None
        and avg_daily_sales_7d is not None
        and avg_daily_sales_7d > 0
    ):
        sales_velocity_ratio = round(sold24h / avg_daily_sales_7d, 3)

    trend = "UNKNOWN"

    rising_signals = 0
    falling_signals = 0
    signal_count = 0

    if sales_growth_7d is not None:
        signal_count += 1
        if sales_growth_7d >= 20:
            rising_signals += 1
        elif sales_growth_7d <= -20:
            falling_signals += 1

    if sales_velocity_ratio is not None:
        signal_count += 1
        if sales_velocity_ratio >= 1.25:
            rising_signals += 1
        elif sales_velocity_ratio <= 0.75:
            falling_signals += 1

    if signal_count > 0:
        if rising_signals > falling_signals:
            trend = "RISING"
        elif falling_signals > rising_signals:
            trend = "FALLING"
        else:
            trend = "STABLE"

    previous_price_snapshot = latest_snapshot_with_value(history, "currentPrice")
    previous_price = (
        as_float(previous_price_snapshot.get("currentPrice"))
        if previous_price_snapshot is not None
        else None
    )

    current_price = as_float(product.get("currentPrice"))
    price_change_percent = safe_percent_change(current_price, previous_price)

    seven_days_ago = now - timedelta(days=7)
    prices_7d: List[float] = []

    for item in history:
        dt = parse_datetime(item.get("timestamp"))
        price = as_float(item.get("currentPrice"))

        if dt is not None and dt >= seven_days_ago and price is not None:
            prices_7d.append(price)

    if current_price is not None:
        prices_7d.append(current_price)

    lowest_price_7d = round(min(prices_7d), 2) if prices_7d else None

    reviews24h = calculate_counter_delta(product.get("reviewCount"), baseline_24h, "reviewCount")
    review_growth_7d = calculate_counter_delta(product.get("reviewCount"), baseline_7d, "reviewCount")
    review_growth_percent_7d = percent_growth_from_counts(
        product.get("reviewCount"),
        baseline_7d,
        "reviewCount",
    )

    new_creators_7d = calculate_regular_delta(
        product.get("creatorCount"),
        baseline_7d,
        "creatorCount",
    )
    creator_growth_7d = percent_growth_from_counts(
        product.get("creatorCount"),
        baseline_7d,
        "creatorCount",
    )

    views7d = calculate_regular_delta(product.get("views"), baseline_7d, "views")
    views_growth_7d = percent_growth_from_counts(
        product.get("views"),
        baseline_7d,
        "views",
    )

    stock_change_24h = calculate_regular_delta(product.get("stock"), baseline_24h, "stock")
    stock_change_7d = calculate_regular_delta(product.get("stock"), baseline_7d, "stock")

    estimated_gmv_24h = None
    estimated_gmv_7d = None

    if current_price is not None:
        if sold24h is not None:
            estimated_gmv_24h = round(current_price * sold24h, 2)
        if sold7d is not None:
            estimated_gmv_7d = round(current_price * sold7d, 2)

    observed_age_days = round(
        max(0.0, (now - first_seen).total_seconds() / 86400.0),
        2,
    )

    listed_at = parse_datetime(product.get("listedAt"))
    product_age_days = None

    if listed_at is not None:
        product_age_days = round(
            max(0.0, (now - listed_at).total_seconds() / 86400.0),
            2,
        )

    return {
        "snapshotAt": now.isoformat(),
        "firstSeen": first_seen.isoformat(),
        "observedAgeDays": observed_age_days,
        "productAgeDays": product_age_days,
        "ageSource": "provider_listed_at" if product_age_days is not None else "first_seen_only",

        "historySnapshotCount": len(history),
        "has24hBaseline": baseline_24h is not None,
        "has7dBaseline": baseline_7d is not None,
        "has14dBaseline": baseline_14d is not None,
        "baseline24hAt": baseline_24h.get("timestamp") if baseline_24h else None,
        "baseline7dAt": baseline_7d.get("timestamp") if baseline_7d else None,
        "baseline14dAt": baseline_14d.get("timestamp") if baseline_14d else None,

        "sold24h": round_or_none(sold24h, 0),
        "sold7d": round_or_none(sold7d, 0),
        "previous7dSales": round_or_none(previous_7d_sales, 0),
        "avgDailySales7d": avg_daily_sales_7d,
        "salesVelocityRatio": sales_velocity_ratio,
        "salesGrowth7d": sales_growth_7d,
        "trend": trend,

        "previousPrice": round_or_none(previous_price, 2),
        "lowestPrice7d": lowest_price_7d,
        "priceChangePercent": price_change_percent,

        "reviews24h": round_or_none(reviews24h, 0),
        "reviewGrowth7d": round_or_none(review_growth_7d, 0),
        "reviewGrowthPercent7d": review_growth_percent_7d,

        "newCreators7d": round_or_none(new_creators_7d, 0),
        "creatorGrowth7d": creator_growth_7d,

        "views7d": round_or_none(views7d, 0),
        "viewsGrowth7d": views_growth_7d,

        "stockChange24h": round_or_none(stock_change_24h, 0),
        "stockChange7d": round_or_none(stock_change_7d, 0),

        "estimatedGMV24h": estimated_gmv_24h,
        "estimatedGMV7d": estimated_gmv_7d,
    }


async def enrich_product_with_history(
    product: Dict[str, Any],
    *,
    history_store,
    now: datetime,
    max_snapshots: int,
    tolerance_hours: float,
    log,
) -> Dict[str, Any]:
    identity = (
        product.get("productId")
        or product.get("productUrl")
        or product.get("title")
    )

    if not identity:
        product["historyError"] = "Product has no stable identity."
        return product

    state_key = f"product-{sha1_text(product.get('region', '') + ':' + str(identity))}"

    try:
        state = await history_store.get_value(state_key)

        if not isinstance(state, dict):
            state = {}

        history = valid_history_items(state.get("history"))

        first_seen = parse_datetime(state.get("firstSeen"))

        if first_seen is None:
            first_seen = now

        metrics = compute_history_metrics(
            product,
            history=history,
            first_seen=first_seen,
            now=now,
            tolerance_hours=tolerance_hours,
        )

        product.update(metrics)
        product["historyStoreKey"] = state_key

        current_snapshot = build_snapshot(product, now)

        history.append(current_snapshot)

        if max_snapshots > 0 and len(history) > max_snapshots:
            history = history[-max_snapshots:]

        new_state = {
            "productId": product.get("productId"),
            "productUrl": product.get("productUrl"),
            "title": product.get("title"),
            "region": product.get("region"),
            "firstSeen": first_seen.isoformat(),
            "lastSeen": now.isoformat(),
            "history": history,
        }

        await history_store.set_value(state_key, new_state)

        # Count includes the snapshot just saved.
        product["historySnapshotCount"] = len(history)

    except Exception as exc:
        product["historyError"] = f"{type(exc).__name__}: {exc}"
        log.warning(
            f"[HISTORY] Failed for {identity}: {product['historyError']}"
        )

    return product


# ---------------------------------------------------------------------------
# Opportunity / top-product score
# ---------------------------------------------------------------------------

def percentile_scores(
    products: List[Dict[str, Any]],
    field: str,
) -> Dict[int, float]:
    values: List[Tuple[int, float]] = []

    for index, product in enumerate(products):
        value = as_float(product.get(field))
        if value is not None:
            values.append((index, value))

    if not values:
        return {}

    sorted_values = sorted(value for _, value in values)
    count = len(sorted_values)
    result: Dict[int, float] = {}

    for index, value in values:
        # Fraction of available products at or below this value.
        rank = sum(1 for candidate in sorted_values if candidate <= value)
        result[index] = rank / count

    return result


def direct_ratio(value: Any, maximum: float) -> Optional[float]:
    number = as_float(value)
    if number is None or maximum <= 0:
        return None
    return max(0.0, min(1.0, number / maximum))


def calculate_top_product_scores(products: List[Dict[str, Any]]) -> None:
    percentile_fields = {
        "sold7d": percentile_scores(products, "sold7d"),
        "salesGrowth7d": percentile_scores(products, "salesGrowth7d"),
        "sold24h": percentile_scores(products, "sold24h"),
        "creatorGrowth7d": percentile_scores(products, "creatorGrowth7d"),
        "views7d": percentile_scores(products, "views7d"),
        "reviewGrowth7d": percentile_scores(products, "reviewGrowth7d"),
        "soldCount": percentile_scores(products, "soldCount"),
    }

    # Total = 1.00
    weights = {
        "sold7d": 0.22,
        "salesGrowth7d": 0.18,
        "sold24h": 0.10,
        "creatorGrowth7d": 0.12,
        "views7d": 0.08,
        "reviewGrowth7d": 0.08,
        "commissionPercent": 0.08,
        "rating": 0.05,
        "discountPercent": 0.04,
        "productAgeDays": 0.03,
        "soldCount": 0.02,
    }

    for index, product in enumerate(products):
        weighted_sum = 0.0
        available_weight = 0.0
        components: Dict[str, Optional[float]] = {}

        def add_component(name: str, score: Optional[float]) -> None:
            nonlocal weighted_sum, available_weight
            components[name] = round(score * 100.0, 2) if score is not None else None

            if score is None:
                return

            weight = weights[name]
            weighted_sum += score * weight
            available_weight += weight

        add_component("sold7d", percentile_fields["sold7d"].get(index))
        add_component("salesGrowth7d", percentile_fields["salesGrowth7d"].get(index))
        add_component("sold24h", percentile_fields["sold24h"].get(index))
        add_component("creatorGrowth7d", percentile_fields["creatorGrowth7d"].get(index))
        add_component("views7d", percentile_fields["views7d"].get(index))
        add_component("reviewGrowth7d", percentile_fields["reviewGrowth7d"].get(index))

        add_component(
            "commissionPercent",
            direct_ratio(product.get("commissionPercent"), 20.0),
        )
        add_component(
            "rating",
            direct_ratio(product.get("rating"), 5.0),
        )
        add_component(
            "discountPercent",
            direct_ratio(product.get("discountPercent"), 50.0),
        )

        age = as_float(product.get("productAgeDays"))
        if age is None:
            newness_score = None
        else:
            newness_score = max(0.0, min(1.0, 1.0 - (age / 90.0)))

        add_component("productAgeDays", newness_score)
        add_component("soldCount", percentile_fields["soldCount"].get(index))

        if available_weight > 0:
            raw_score = (weighted_sum / available_weight) * 100.0
            coverage = available_weight
            # Confidence-adjust the score. A first run with little historical
            # data cannot look as strong as a product with full 7/14-day data.
            final_score = raw_score * math.sqrt(coverage)

            product["rawOpportunityScore"] = round(raw_score, 2)
            product["scoreCoveragePercent"] = round(coverage * 100.0, 2)
            product["topProductScore"] = round(final_score, 2)
        else:
            product["rawOpportunityScore"] = None
            product["scoreCoveragePercent"] = 0.0
            product["topProductScore"] = None

        product["scoreComponents"] = components


# ---------------------------------------------------------------------------
# Filtering / sorting
# ---------------------------------------------------------------------------

def metric_at_least(product: Dict[str, Any], field: str, minimum: float) -> bool:
    if minimum <= 0:
        return True

    value = as_float(product.get(field))
    return value is not None and value >= minimum


def product_passes_filters(
    product: Dict[str, Any],
    *,
    only_discounted: bool,
    only_in_stock: bool,
    min_discount: float,
    min_sold_total: int,
    min_sold_24h: int,
    min_sold_7d: int,
    min_rating: float,
    min_commission: float,
) -> bool:
    if only_discounted:
        discount = as_float(product.get("discountPercent"))
        if discount is None or discount <= 0:
            return False

    if only_in_stock and product.get("inStock") is not True:
        return False

    if not metric_at_least(product, "discountPercent", min_discount):
        return False

    if not metric_at_least(product, "soldCount", min_sold_total):
        return False

    if not metric_at_least(product, "sold24h", min_sold_24h):
        return False

    if not metric_at_least(product, "sold7d", min_sold_7d):
        return False

    if not metric_at_least(product, "rating", min_rating):
        return False

    if not metric_at_least(product, "commissionPercent", min_commission):
        return False

    return True


SORT_FIELD_MAP = {
    "top_score": "topProductScore",
    "sold24h": "sold24h",
    "sold7d": "sold7d",
    "sales_growth": "salesGrowth7d",
    "creator_growth": "creatorGrowth7d",
    "views7d": "views7d",
    "review_growth": "reviewGrowth7d",
    "commission": "commissionPercent",
    "rating": "rating",
    "discount": "discountPercent",
    "sold_total": "soldCount",
    "price": "currentPrice",
}


def sort_products(products: List[Dict[str, Any]], sort_by: str) -> List[Dict[str, Any]]:
    field = SORT_FIELD_MAP.get(sort_by, "topProductScore")

    return sorted(
        products,
        key=lambda product: (
            as_float(product.get(field))
            if as_float(product.get(field)) is not None
            else float("-inf"),
            as_float(product.get("soldCount")) or 0.0,
        ),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# v6 finalization, quality and scoring (overrides v5 ranking functions)
# ---------------------------------------------------------------------------

STRATEGIC_FIELDS = (
    "sold24h", "sold7d", "currentPrice", "rating", "reviewCount",
    "creatorCount", "videoCount", "videoViews7d", "commissionPercent",
    "stock", "productAgeDays",
)


def finalize_v6_metrics(product: Dict[str, Any], now: datetime) -> None:
    history_sold24h = as_float(product.get("sold24h"))
    history_sold7d = as_float(product.get("sold7d"))
    history_sales_growth = as_float(product.get("salesGrowth7d"))

    product["historySold24h"] = round_or_none(history_sold24h, 0)
    product["historySold7d"] = round_or_none(history_sold7d, 0)
    product["historySalesGrowth7d"] = history_sales_growth

    market_sold24h = as_float(product.get("marketSold24h"))
    market_sold7d = as_float(product.get("marketSold7d"))

    if market_sold24h is not None:
        product["sold24h"] = int(round(market_sold24h))
        product["sold24hSource"] = "market_intelligence"
    elif history_sold24h is not None:
        product["sold24h"] = int(round(history_sold24h))
        product["sold24hSource"] = "own_history"
    else:
        product["sold24h"] = None
        product["sold24hSource"] = None

    if market_sold7d is not None:
        product["sold7d"] = int(round(market_sold7d))
        product["sold7dSource"] = "market_intelligence"
    elif history_sold7d is not None:
        product["sold7d"] = int(round(history_sold7d))
        product["sold7dSource"] = "own_history"
    else:
        product["sold7d"] = None
        product["sold7dSource"] = None

    sold24h = as_float(product.get("sold24h"))
    sold7d = as_float(product.get("sold7d"))
    product["avgDailySales7d"] = round(sold7d / 7, 2) if sold7d is not None else None
    product["salesVelocityRatio"] = (
        round(sold24h / (sold7d / 7), 3)
        if sold24h is not None and sold7d is not None and sold7d > 0
        else None
    )

    revenue_growth = as_float(product.get("marketRevenueGrowthPercent7d"))
    product["revenueGrowth7d"] = revenue_growth
    product["revenueGrowth7dSource"] = "market_intelligence" if revenue_growth is not None else None
    product["salesGrowth7d"] = history_sales_growth
    product["salesGrowth7dSource"] = "own_history" if history_sales_growth is not None else None

    listed_at = parse_datetime(product.get("listedAt"))
    if listed_at is not None:
        product["productAgeDays"] = round(max(0, (now - listed_at).total_seconds() / 86400), 2)
        product["ageSource"] = "market_launch_date_or_provider"

    if product.get("creatorCount") is None and product.get("creatorCountObserved7d") is not None:
        product["creatorCount"] = product["creatorCountObserved7d"]
        product["creatorCountSource"] = "creator_observation_7d"
    if product.get("videoCount") is None and product.get("videoCountObserved7d") is not None:
        product["videoCount"] = product["videoCountObserved7d"]
        product["videoCountSource"] = "video_observation_7d"
    if product.get("views") is None and product.get("videoViews7d") is not None:
        product["views"] = product["videoViews7d"]
        product["viewsSource"] = "video_observation_7d"

    if product.get("videoEngagementRate7d") is not None:
        product["engagementRate"] = product["videoEngagementRate7d"]

    trend_signals: List[float] = []
    velocity = as_float(product.get("salesVelocityRatio"))
    if velocity is not None:
        trend_signals.append((velocity - 1.0) * 100)
    if history_sales_growth is not None:
        trend_signals.append(history_sales_growth)
    if revenue_growth is not None:
        trend_signals.append(revenue_growth)

    if trend_signals:
        strength = sum(trend_signals) / len(trend_signals)
        product["trendStrength"] = round(strength, 2)
        product["trend"] = "RISING" if strength >= 15 else "FALLING" if strength <= -15 else "STABLE"
    else:
        product["trendStrength"] = None
        product["trend"] = "UNKNOWN"

    current_price = as_float(product.get("currentPrice"))
    product["estimatedGMVLocal24h"] = (
        round(current_price * sold24h, 2)
        if current_price is not None and sold24h is not None else None
    )
    product["estimatedGMVLocal7d"] = (
        round(current_price * sold7d, 2)
        if current_price is not None and sold7d is not None else None
    )

    present = sum(1 for field in STRATEGIC_FIELDS if product.get(field) is not None)
    product["dataCoveragePercent"] = round((present / len(STRATEGIC_FIELDS)) * 100, 2)
    product["missingStrategicFields"] = [field for field in STRATEGIC_FIELDS if product.get(field) is None]

    flags: List[str] = []
    if product.get("currentPrice") is None:
        flags.append("NO_LOCAL_CURRENT_PRICE")
    raw_price = as_float(product.get("priceRaw"))
    if product.get("priceNormalizationMode") == "auto" and raw_price is not None and raw_price < 100 and abs(raw_price-round(raw_price)) < 1e-9:
        flags.append("SMALL_INTEGER_PRICE_VERIFY")
    if product.get("soldCount") is None and product.get("sold7d") is None:
        flags.append("NO_SALES_SIGNAL")
    if product.get("commissionPercent") is None:
        flags.append("NO_COMMISSION_DATA")
    if product.get("stock") is None:
        flags.append("NO_STOCK_DATA")
    if product.get("creatorCount") is None:
        flags.append("NO_CREATOR_DATA")
    if product.get("videoViews7d") is None:
        flags.append("NO_VIDEO_DATA")
    product["dataQualityFlags"] = flags


def percentile_scores_v6(products: List[Dict[str, Any]], field: str) -> Dict[int, float]:
    pairs = [(i, as_float(p.get(field))) for i, p in enumerate(products)]
    pairs = [(i, value) for i, value in pairs if value is not None]
    if not pairs:
        return {}
    ordered = sorted(value for _, value in pairs)
    n = len(ordered)
    return {i: sum(1 for candidate in ordered if candidate <= value) / n for i, value in pairs}


def calculate_top_product_scores(products: List[Dict[str, Any]]) -> None:
    percentile_fields = {
        field: percentile_scores_v6(products, field)
        for field in (
            "sold7d", "sold24h", "salesVelocityRatio", "revenueGrowth7d",
            "creatorCount", "creatorGrowth7d", "videoViews7d",
            "reviewGrowth7d", "soldCount",
        )
    }
    weights = {
        "sold7d": 0.20,
        "sold24h": 0.08,
        "salesVelocityRatio": 0.10,
        "revenueGrowth7d": 0.10,
        "creatorCount": 0.08,
        "creatorGrowth7d": 0.06,
        "videoViews7d": 0.08,
        "engagementRate": 0.05,
        "commissionPercent": 0.08,
        "rating": 0.04,
        "reviewGrowth7d": 0.04,
        "discountPercent": 0.03,
        "productAgeDays": 0.03,
        "stockAvailability": 0.01,
        "soldCount": 0.02,
    }
    for index, product in enumerate(products):
        total = 0.0
        coverage = 0.0
        components: Dict[str, Optional[float]] = {}

        def add(name: str, score: Optional[float]) -> None:
            nonlocal total, coverage
            components[name] = round(score * 100, 2) if score is not None else None
            if score is not None:
                total += score * weights[name]
                coverage += weights[name]

        for field in (
            "sold7d", "sold24h", "salesVelocityRatio", "revenueGrowth7d",
            "creatorCount", "creatorGrowth7d", "videoViews7d",
            "reviewGrowth7d", "soldCount",
        ):
            add(field, percentile_fields[field].get(index))

        add("engagementRate", direct_ratio(product.get("engagementRate"), 10.0))
        add("commissionPercent", direct_ratio(product.get("commissionPercent"), 20.0))
        add("rating", direct_ratio(product.get("rating"), 5.0))
        add("discountPercent", direct_ratio(product.get("discountPercent"), 50.0))
        age = as_float(product.get("productAgeDays"))
        add("productAgeDays", None if age is None else max(0, min(1, 1 - age / 180)))

        stock_score = None
        if product.get("inStock") is True:
            stock_score = 1.0
        elif product.get("inStock") is False:
            stock_score = 0.0
        elif as_float(product.get("stock")) is not None:
            stock_score = 1.0 if as_float(product.get("stock")) > 0 else 0.0
        add("stockAvailability", stock_score)

        if coverage > 0:
            raw_score = total / coverage * 100
            product["rawOpportunityScore"] = round(raw_score, 2)
            product["scoreCoveragePercent"] = round(coverage * 100, 2)
            product["topProductScore"] = round(raw_score * math.sqrt(coverage), 2)
        else:
            product["rawOpportunityScore"] = None
            product["scoreCoveragePercent"] = 0.0
            product["topProductScore"] = None
        product["scoreComponents"] = components


def product_passes_filters(
    product: Dict[str, Any],
    *,
    only_discounted: bool,
    only_in_stock: bool,
    min_discount: float,
    min_sold_total: int,
    min_sold_24h: int,
    min_sold_7d: int,
    min_rating: float,
    min_commission: float,
    min_creator_count: int = 0,
    min_video_views_7d: int = 0,
    min_top_score: float = 0,
) -> bool:
    if only_discounted:
        discount = as_float(product.get("discountPercent"))
        if discount is None or discount <= 0:
            return False
    if only_in_stock and product.get("inStock") is not True:
        return False
    checks = (
        ("discountPercent", min_discount),
        ("soldCount", min_sold_total),
        ("sold24h", min_sold_24h),
        ("sold7d", min_sold_7d),
        ("rating", min_rating),
        ("commissionPercent", min_commission),
        ("creatorCount", min_creator_count),
        ("videoViews7d", min_video_views_7d),
        ("topProductScore", min_top_score),
    )
    return all(metric_at_least(product, field, minimum) for field, minimum in checks)


SORT_FIELD_MAP = {
    "top_score": "topProductScore",
    "sold24h": "sold24h",
    "sold7d": "sold7d",
    "sales_velocity": "salesVelocityRatio",
    "revenue_growth": "revenueGrowth7d",
    "creator_count": "creatorCount",
    "creator_growth": "creatorGrowth7d",
    "views7d": "videoViews7d",
    "engagement": "engagementRate",
    "commission": "commissionPercent",
    "rating": "rating",
    "discount": "discountPercent",
    "sold_total": "soldCount",
    "price": "currentPrice",
}


def sort_products(products: List[Dict[str, Any]], sort_by: str) -> List[Dict[str, Any]]:
    field = SORT_FIELD_MAP.get(sort_by, "topProductScore")
    def key(product: Dict[str, Any]) -> Tuple[float, float, float]:
        primary = as_float(product.get(field))
        sold7d = as_float(product.get("sold7d"))
        sold_total = as_float(product.get("soldCount"))
        return (
            primary if primary is not None else float("-inf"),
            sold7d if sold7d is not None else float("-inf"),
            sold_total if sold_total is not None else float("-inf"),
        )
    return sorted(products, key=key, reverse=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    async with Actor:
        log = Actor.log
        input_data = await Actor.get_input() or {}
        now = datetime.now(timezone.utc)

        region = str(input_data.get("region", "DE")).strip().upper() or "DE"
        search_actor_id = str(input_data.get("searchActorId", DEFAULT_SEARCH_ACTOR_ID)).strip() or DEFAULT_SEARCH_ACTOR_ID
        market_actor_id = str(input_data.get("marketActorId", DEFAULT_MARKET_ACTOR_ID)).strip() or DEFAULT_MARKET_ACTOR_ID
        creators_actor_id = str(input_data.get("creatorsActorId", DEFAULT_CREATORS_ACTOR_ID)).strip() or DEFAULT_CREATORS_ACTOR_ID
        videos_actor_id = str(input_data.get("videosActorId", DEFAULT_VIDEOS_ACTOR_ID)).strip() or DEFAULT_VIDEOS_ACTOR_ID

        keywords = input_data.get("keywords") or []
        categories = input_data.get("categories") or []
        if isinstance(keywords, str):
            keywords = [keywords]
        if isinstance(categories, str):
            categories = [categories]
        keywords = [str(x).strip() for x in keywords if str(x).strip()]
        categories = [str(x).strip() for x in categories if str(x).strip()]

        search_queries = list(dict.fromkeys(
            keywords + [CATEGORY_QUERY_MAP.get(cat.lower(), cat) for cat in categories]
        ))
        overall_top = bool(input_data.get("marketOverallTop", False))
        if not search_queries and not overall_top:
            raise ValueError("Add at least one keyword/category or enable Market overall top.")

        max_search_items = max(1, min(1000, int(input_data.get("maxSearchItemsPerQuery", 100))))
        max_market_items = max(1, min(1000, int(input_data.get("maxMarketItemsPerSource", 50))))
        max_output_products = max(1, min(1000, int(input_data.get("maxOutputProducts", 100))))
        max_enrichment_products = max(1, min(100, int(input_data.get("maxEnrichmentProducts", 5))))
        max_creators_per_product = max(1, min(100, int(input_data.get("maxCreatorsPerProduct", 20))))
        max_videos_per_product = max(1, min(100, int(input_data.get("maxVideosPerProduct", 20))))

        price_mode = str(input_data.get("priceMode", "auto")).strip().lower()
        if price_mode not in {"auto", "raw", "minor_units"}:
            raise ValueError("priceMode must be auto, raw, or minor_units.")
        price_divisor = float(input_data.get("priceDivisor", 100) or 100)
        auto_minor_threshold = float(input_data.get("autoMinorUnitThreshold", 100) or 100)
        include_raw_provider_data = bool(input_data.get("includeRawProviderData", False))

        errors: List[Dict[str, str]] = []
        all_search_products: List[Dict[str, Any]] = []

        # 1) Base search: local price, total sold, rating, shop, image.
        for query in search_queries:
            rows, error = await call_search_provider(
                query=query,
                region=region,
                max_items=max_search_items,
                search_actor_id=search_actor_id,
                price_mode=price_mode,
                price_divisor=price_divisor,
                auto_minor_threshold=auto_minor_threshold,
                include_raw_provider_data=include_raw_provider_data,
                log=log,
            )
            all_search_products.extend(rows)
            if error:
                errors.append({"stage": "search", "query": query, "error": error})

        products = deduplicate_products(all_search_products)
        for product in products:
            if not product.get("dataSources"):
                product["dataSources"] = [product.get("source") or search_actor_id]

        # 2) Direct recent-market intelligence for last 24h and last 7d.
        market_enabled = bool(input_data.get("enableMarketIntelligence", True))
        market_paid_analytics = bool(input_data.get("enableMarketPaidAnalytics", False))
        include_market_only = bool(input_data.get("includeMarketOnlyProducts", True))
        market_rows_24h: List[Dict[str, Any]] = []
        market_rows_7d: List[Dict[str, Any]] = []

        if market_enabled:
            market_rows_24h, err = await call_market_intelligence(
                actor_id=market_actor_id,
                keywords=keywords,
                categories=categories,
                region=region,
                period="lastDay",
                max_results_per_source=max_market_items,
                add_analytics=market_paid_analytics,
                overall_top=overall_top,
                log=log,
            )
            if err:
                errors.append({"stage": "market_lastDay", "error": err})

            market_rows_7d, err = await call_market_intelligence(
                actor_id=market_actor_id,
                keywords=keywords,
                categories=categories,
                region=region,
                period="last7Day",
                max_results_per_source=max_market_items,
                add_analytics=market_paid_analytics,
                overall_top=overall_top,
                log=log,
            )
            if err:
                errors.append({"stage": "market_last7Day", "error": err})

            products = merge_market_rows_into_products(
                products,
                rows=market_rows_24h,
                period="lastDay",
                actor_id=market_actor_id,
                region=region,
                include_market_only_products=include_market_only,
            )
            products = merge_market_rows_into_products(
                products,
                rows=market_rows_7d,
                period="last7Day",
                actor_id=market_actor_id,
                region=region,
                include_market_only_products=include_market_only,
            )

        # 3) Optional creator/video intelligence only for the strongest products.
        enrichment_products = select_enrichment_products(products, max_enrichment_products)
        product_inputs = [
            str(product.get("productUrl") or product.get("productId"))
            for product in enrichment_products
            if product.get("productUrl") or product.get("productId")
        ]
        creator_rows: List[Dict[str, Any]] = []
        video_rows: List[Dict[str, Any]] = []

        if bool(input_data.get("enableCreatorEnrichment", False)):
            creator_rows, err = await call_creators_enrichment(
                actor_id=creators_actor_id,
                product_inputs=product_inputs,
                region=region,
                max_results_per_product=max_creators_per_product,
                log=log,
            )
            if err:
                errors.append({"stage": "creators", "error": err})

        if bool(input_data.get("enableVideoEnrichment", False)):
            video_rows, err = await call_videos_enrichment(
                actor_id=videos_actor_id,
                product_inputs=product_inputs,
                region=region,
                max_results_per_product=max_videos_per_product,
                log=log,
            )
            if err:
                errors.append({"stage": "videos", "error": err})

        merge_creator_video_aggregates(
            products,
            creator_aggregates=aggregate_creators(creator_rows),
            video_aggregates=aggregate_videos(video_rows),
            creators_actor_id=creators_actor_id,
            videos_actor_id=videos_actor_id,
        )

        # 4) Own persistent snapshots remain a second, independent history source.
        track_history = bool(input_data.get("trackHistory", True))
        history_store_name = sanitize_store_name(
            str(input_data.get("historyStoreName", DEFAULT_HISTORY_STORE))
        )
        max_snapshots = max(10, min(5000, int(input_data.get("maxSnapshotsPerProduct", 500))))
        tolerance_hours = max(1.0, min(72.0, float(input_data.get("windowToleranceHours", 18) or 18)))

        if track_history:
            history_store = await Actor.open_key_value_store(name=history_store_name)
            for product in products:
                await enrich_product_with_history(
                    product,
                    history_store=history_store,
                    now=now,
                    max_snapshots=max_snapshots,
                    tolerance_hours=tolerance_hours,
                    log=log,
                )
        else:
            for product in products:
                product.update({
                    "snapshotAt": now.isoformat(),
                    "firstSeen": None,
                    "observedAgeDays": None,
                    "productAgeDays": None,
                    "sold24h": None,
                    "sold7d": None,
                    "salesGrowth7d": None,
                    "previousPrice": None,
                    "lowestPrice7d": product.get("currentPrice"),
                    "priceChangePercent": None,
                    "reviewGrowth7d": None,
                    "creatorGrowth7d": None,
                    "stockChange24h": None,
                    "stockChange7d": None,
                })

        # 5) Prefer direct market windows, keep own-history values as diagnostics.
        for product in products:
            product["snapshotAt"] = now.isoformat()
            finalize_v6_metrics(product, now)

        calculate_top_product_scores(products)

        only_discounted = bool(input_data.get("onlyDiscounted", False))
        only_in_stock = bool(input_data.get("onlyInStock", False))
        filtered_products = [
            product for product in products
            if product_passes_filters(
                product,
                only_discounted=only_discounted,
                only_in_stock=only_in_stock,
                min_discount=float(input_data.get("minDiscount", 0) or 0),
                min_sold_total=int(input_data.get("minSoldTotal", 0) or 0),
                min_sold_24h=int(input_data.get("minSold24h", 0) or 0),
                min_sold_7d=int(input_data.get("minSold7d", 0) or 0),
                min_rating=float(input_data.get("minRating", 0) or 0),
                min_commission=float(input_data.get("minCommissionPercent", 0) or 0),
                min_creator_count=int(input_data.get("minCreatorCount", 0) or 0),
                min_video_views_7d=int(input_data.get("minVideoViews7d", 0) or 0),
                min_top_score=float(input_data.get("minTopProductScore", 0) or 0),
            )
        ]

        sort_by = str(input_data.get("sortBy", "top_score")).strip().lower()
        output_products = sort_products(filtered_products, sort_by)[:max_output_products]
        for rank, product in enumerate(output_products, start=1):
            product["rank"] = rank

        if output_products:
            await Actor.push_data(output_products)

        summary = {
            "version": VERSION,
            "runAt": now.isoformat(),
            "region": region,
            "queries": search_queries,
            "marketOverallTop": overall_top,
            "searchActorId": search_actor_id,
            "marketActorId": market_actor_id,
            "creatorsActorId": creators_actor_id,
            "videosActorId": videos_actor_id,
            "baseSearchRows": len(all_search_products),
            "marketRows24h": len(market_rows_24h),
            "marketRows7d": len(market_rows_7d),
            "creatorRows": len(creator_rows),
            "videoRows": len(video_rows),
            "uniqueProductsTracked": len(products),
            "productsAfterFilters": len(filtered_products),
            "productsOutput": len(output_products),
            "sortBy": sort_by,
            "trackHistory": track_history,
            "historyStoreName": history_store_name if track_history else None,
            "marketIntelligenceEnabled": market_enabled,
            "marketPaidAnalyticsEnabled": market_paid_analytics,
            "creatorEnrichmentEnabled": bool(input_data.get("enableCreatorEnrichment", False)),
            "videoEnrichmentEnabled": bool(input_data.get("enableVideoEnrichment", False)),
            "notes": {
                "sold24h": "Prefers market-intelligence lastDay unitsSold; falls back to own history.",
                "sold7d": "Prefers market-intelligence last7Day unitsSold; falls back to own history.",
                "commissionPercent": "Can come directly from market intelligence.",
                "creatorCount": "Can come from optional analytics or observed creator rows.",
                "videoViews7d": "Comes from optional product-video enrichment.",
                "stock": "Never invented; remains null unless a source exposes inventory.",
                "money": "currentPrice is local-currency search data; marketRevenueUSD* and marketAvgSellingPriceUSD* are explicitly USD.",
            },
            "errors": errors,
        }
        await Actor.set_value("RUN_SUMMARY", summary)

        log.info(
            "[DONE] "
            f"base={len(all_search_products)} market24={len(market_rows_24h)} "
            f"market7d={len(market_rows_7d)} creators={len(creator_rows)} "
            f"videos={len(video_rows)} unique={len(products)} output={len(output_products)}"
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
