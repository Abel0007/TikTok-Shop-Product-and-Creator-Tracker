#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from apify import Actor


DEFAULT_SEARCH_ACTOR_ID = "getanyapi/tiktok-shop-scraper"
DEFAULT_HISTORY_STORE = "tiktok-shop-top-products-history"

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
        "searchQuery": query,
        "searchQueries": [query],
        "region": region.upper(),
        "productId": str(raw.get("productId")) if raw.get("productId") is not None else None,
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
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    async with Actor:
        log = Actor.log
        input_data = await Actor.get_input() or {}

        region = str(input_data.get("region", "DE")).strip().upper() or "DE"

        search_actor_id = (
            str(input_data.get("searchActorId", DEFAULT_SEARCH_ACTOR_ID)).strip()
            or DEFAULT_SEARCH_ACTOR_ID
        )

        keywords = input_data.get("keywords") or []
        categories = input_data.get("tiktokCategories") or []

        if isinstance(keywords, str):
            keywords = [keywords]

        if isinstance(categories, str):
            categories = [categories]

        search_queries: List[str] = []

        for keyword in keywords:
            value = str(keyword).strip()
            if value:
                search_queries.append(value)

        for category in categories:
            value = str(category).strip()
            if not value:
                continue
            search_queries.append(
                CATEGORY_QUERY_MAP.get(value.lower(), value)
            )

        search_queries = list(dict.fromkeys(search_queries))

        if not search_queries:
            raise ValueError(
                "Add at least one keyword or TikTok category."
            )

        max_items = max(1, min(1000, int(input_data.get("maxItems", 100))))
        max_output_products = max(
            1,
            min(1000, int(input_data.get("maxOutputProducts", 100))),
        )

        price_mode = str(input_data.get("priceMode", "auto")).strip().lower()
        if price_mode not in {"auto", "raw", "minor_units"}:
            raise ValueError("priceMode must be auto, raw, or minor_units.")

        price_divisor = float(input_data.get("priceDivisor", 100) or 100)
        auto_minor_threshold = float(
            input_data.get("autoMinorUnitThreshold", 100) or 100
        )

        include_raw_provider_data = bool(
            input_data.get("includeRawProviderData", False)
        )

        track_history = bool(input_data.get("trackHistory", True))
        history_store_name = sanitize_store_name(
            str(input_data.get("historyStoreName", DEFAULT_HISTORY_STORE))
        )
        max_snapshots = max(
            10,
            min(5000, int(input_data.get("maxSnapshotsPerProduct", 500))),
        )
        tolerance_hours = max(
            1.0,
            min(72.0, float(input_data.get("windowToleranceHours", 18) or 18)),
        )

        now = datetime.now(timezone.utc)

        all_products: List[Dict[str, Any]] = []
        search_errors: List[Dict[str, str]] = []

        for query in search_queries:
            products, error = await call_search_provider(
                query=query,
                region=region,
                max_items=max_items,
                search_actor_id=search_actor_id,
                price_mode=price_mode,
                price_divisor=price_divisor,
                auto_minor_threshold=auto_minor_threshold,
                include_raw_provider_data=include_raw_provider_data,
                log=log,
            )

            all_products.extend(products)

            if error:
                search_errors.append({"query": query, "error": error})

        products = deduplicate_products(all_products)

        history_store = None

        if track_history:
            history_store = await Actor.open_key_value_store(
                name=history_store_name
            )

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
                product.update(
                    {
                        "snapshotAt": now.isoformat(),
                        "firstSeen": None,
                        "observedAgeDays": None,
                        "productAgeDays": (
                            round(
                                max(
                                    0.0,
                                    (
                                        now
                                        - parse_datetime(product.get("listedAt"))
                                    ).total_seconds()
                                    / 86400.0,
                                ),
                                2,
                            )
                            if parse_datetime(product.get("listedAt")) is not None
                            else None
                        ),
                        "ageSource": (
                            "provider_listed_at"
                            if parse_datetime(product.get("listedAt")) is not None
                            else None
                        ),
                        "sold24h": None,
                        "sold7d": None,
                        "avgDailySales7d": None,
                        "salesGrowth7d": None,
                        "trend": "UNKNOWN",
                        "previousPrice": None,
                        "lowestPrice7d": product.get("currentPrice"),
                        "priceChangePercent": None,
                        "reviews24h": None,
                        "reviewGrowth7d": None,
                        "creatorGrowth7d": None,
                        "newCreators7d": None,
                        "views7d": None,
                        "viewsGrowth7d": None,
                        "stockChange24h": None,
                        "stockChange7d": None,
                    }
                )

        calculate_top_product_scores(products)

        only_discounted = bool(input_data.get("onlyDiscounted", False))
        only_in_stock = bool(input_data.get("onlyInStock", False))

        min_discount = float(input_data.get("minDiscount", 0) or 0)
        min_sold_total = int(input_data.get("minSoldTotal", 0) or 0)
        min_sold_24h = int(input_data.get("minSold24h", 0) or 0)
        min_sold_7d = int(input_data.get("minSold7d", 0) or 0)
        min_rating = float(input_data.get("minRating", 0) or 0)
        min_commission = float(input_data.get("minCommissionPercent", 0) or 0)

        filtered_products = [
            product
            for product in products
            if product_passes_filters(
                product,
                only_discounted=only_discounted,
                only_in_stock=only_in_stock,
                min_discount=min_discount,
                min_sold_total=min_sold_total,
                min_sold_24h=min_sold_24h,
                min_sold_7d=min_sold_7d,
                min_rating=min_rating,
                min_commission=min_commission,
            )
        ]

        sort_by = str(input_data.get("sortBy", "top_score")).strip().lower()

        sorted_products = sort_products(filtered_products, sort_by)
        output_products = sorted_products[:max_output_products]

        for rank, product in enumerate(output_products, start=1):
            product["rank"] = rank

        if output_products:
            await Actor.push_data(output_products)

        summary = {
            "version": "5.0-data",
            "runAt": now.isoformat(),
            "region": region,
            "queries": search_queries,
            "searchActorId": search_actor_id,
            "productsReceived": len(all_products),
            "uniqueProductsTracked": len(products),
            "productsAfterFilters": len(filtered_products),
            "productsOutput": len(output_products),
            "sortBy": sort_by,
            "trackHistory": track_history,
            "historyStoreName": history_store_name if track_history else None,
            "priceMode": price_mode,
            "priceDivisor": price_divisor,
            "autoMinorUnitThreshold": auto_minor_threshold,
            "filters": {
                "onlyDiscounted": only_discounted,
                "onlyInStock": only_in_stock,
                "minDiscount": min_discount,
                "minSoldTotal": min_sold_total,
                "minSold24h": min_sold_24h,
                "minSold7d": min_sold_7d,
                "minRating": min_rating,
                "minCommissionPercent": min_commission,
            },
            "historicalMetrics": {
                "sold24h": "Requires a suitable ~24h baseline.",
                "sold7d": "Requires a suitable ~7d baseline.",
                "salesGrowth7d": "Requires ~7d and ~14d baselines.",
                "previousPrice": "Available after at least one earlier snapshot with price.",
                "lowestPrice7d": "Calculated from stored price snapshots in the last 7 days.",
                "creatorGrowth7d": "Requires creatorCount from provider plus a ~7d baseline.",
                "views7d": "Requires views from provider plus a ~7d baseline.",
                "stockChange7d": "Requires stock from provider plus a ~7d baseline.",
            },
            "errors": search_errors,
        }

        await Actor.set_value("SEARCH_SUMMARY", summary)

        log.info(
            "[DONE] "
            f"received={len(all_products)} "
            f"unique={len(products)} "
            f"output={len(output_products)} "
            f"history={track_history}"
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
