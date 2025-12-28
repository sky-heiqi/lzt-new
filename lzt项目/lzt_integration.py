# -*- coding: utf-8 -*-
"""
LZT Market (lzt.market) integration helpers.

Goals:
- Search items from a category endpoint (e.g. /steam) on https://prod-api.lzt.market
- Score & pick the best item by title/price/seller constraints
- Buy via POST /{item_id}/fast-buy with safe retries

Notes:
- Official docs mention rate limit 300 req/min and recommend ~0.2s delay between requests.
- This module keeps retries conservative by default. Tune via request fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union
import time
import random
import requests


DEFAULT_BASE_URL = "https://prod-api.lzt.market"
DEFAULT_TIMEOUT = 15.0
DEFAULT_MIN_DELAY_SEC = 0.20


@dataclass
class LztSearchRules:
    # Title matching
    include_keywords: List[str] = field(default_factory=list)   # all must match (case-insensitive substring)
    any_keywords: List[str] = field(default_factory=list)       # at least one must match
    exclude_keywords: List[str] = field(default_factory=list)

    # Price
    min_price: Optional[float] = None
    max_price: Optional[float] = None

    # Seller constraints (best-effort, depends on returned json fields)
    seller_whitelist: List[str] = field(default_factory=list)   # usernames (case-insensitive)
    seller_blacklist: List[str] = field(default_factory=list)
    min_seller_rating: Optional[float] = None                   # numeric rating if present
    min_seller_reviews: Optional[int] = None                    # count if present

    # General
    require_in_stock: bool = True


@dataclass
class LztAutoBuyConfig:
    category: str
    # Raw query params sent to the category list endpoint, e.g. {"page": 1, "sort_by": "price_to_up", ...}
    search_params: Dict[str, Any] = field(default_factory=dict)

    rules: LztSearchRules = field(default_factory=LztSearchRules)

    # How many pages to scan (category endpoints are paginated)
    max_pages: int = 3
    per_page_delay_sec: float = DEFAULT_MIN_DELAY_SEC

    # Purchase retries
    buy_attempts: int = 10
    buy_delay_sec: float = DEFAULT_MIN_DELAY_SEC

    # HTTP behavior
    timeout_sec: float = DEFAULT_TIMEOUT
    max_http_retries: int = 3  # for transient 5xx/429
    base_url: str = DEFAULT_BASE_URL


class LZTMarketError(RuntimeError):
    pass


def _lc(s: Any) -> str:
    return str(s or "").strip().lower()


def _get_nested(obj: Any, paths: List[List[Union[str, int]]]) -> Any:
    """Try multiple candidate paths for nested dict/list structures."""
    for path in paths:
        cur = obj
        ok = True
        for key in path:
            try:
                if isinstance(key, int):
                    cur = cur[key]
                else:
                    cur = cur.get(key) if isinstance(cur, dict) else None
                if cur is None:
                    ok = False
                    break
            except Exception:
                ok = False
                break
        if ok:
            return cur
    return None


class LZTMarketClient:
    def __init__(self, token: str, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT, min_delay_sec: float = DEFAULT_MIN_DELAY_SEC):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.min_delay_sec = max(0.0, float(min_delay_sec))
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "skyheqii-bot/1.0"
        })

    def _sleep(self, sec: float):
        if sec <= 0:
            return
        # add small jitter to reduce bursty traffic
        time.sleep(sec + random.uniform(0, sec * 0.15))

    def request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None, json_body: Any = None, timeout: Optional[float] = None) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        timeout = self.timeout if timeout is None else timeout

        last_err = None
        for attempt in range(1, 1 + 3):
            try:
                resp = self.session.request(method.upper(), url, params=params, json=json_body, timeout=timeout)
                # basic backoff for rate limit or transient server errors
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
                    # Respect Retry-After if present
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        self._sleep(float(retry_after))
                    else:
                        self._sleep(max(self.min_delay_sec, attempt * self.min_delay_sec))
                    continue

                resp.raise_for_status()
                if resp.content:
                    return resp.json()
                return None
            except requests.RequestException as e:
                last_err = str(e)
                self._sleep(max(self.min_delay_sec, attempt * self.min_delay_sec))
                continue

        raise LZTMarketError(f"Request failed after retries: {method} {url}. Last error: {last_err}")

    def list_category(self, category: str, params: Dict[str, Any]) -> Any:
        self._sleep(self.min_delay_sec)
        return self.request("GET", f"/{category.lstrip('/')}", params=params)

    def fast_buy(self, item_id: Union[int, str], *, attempts: int = 10, delay_sec: float = DEFAULT_MIN_DELAY_SEC) -> Any:
        """POST /{item_id}/fast-buy with retries for 'retry_request' style errors."""
        attempts = max(1, int(attempts))
        delay_sec = max(0.0, float(delay_sec))

        last_payload = None
        for i in range(1, attempts + 1):
            self._sleep(delay_sec)
            payload = self.request("POST", f"/{item_id}/fast-buy")
            last_payload = payload

            # docs: error could be "retry_request" (requires waiting and retrying)
            # we handle it permissively (best-effort) because exact shape may vary.
            err_code = _get_nested(payload, [["error", "code"], ["errors", 0, "code"], ["code"]])
            err_msg = _get_nested(payload, [["error", "message"], ["message"], ["errors", 0, "message"]])

            if err_code and str(err_code) == "retry_request":
                continue

            # some APIs return {"success": false, ...}
            success = _get_nested(payload, [["success"], ["status"]])
            if isinstance(success, bool) and not success:
                # retry on temporary-ish message
                if err_msg and "retry" in _lc(err_msg):
                    continue

            return payload

        raise LZTMarketError(f"Fast-buy failed after {attempts} attempts. Last response: {str(last_payload)[:500]}")

    @staticmethod
    def extract_items(payload: Any) -> List[Dict[str, Any]]:
        """Best-effort extraction of item list from category response."""
        if payload is None:
            return []
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        # common shapes:
        candidates = _get_nested(payload, [["items"], ["data", "items"], ["data"], ["response", "items"], ["response", "data"]])
        if isinstance(candidates, list):
            return [x for x in candidates if isinstance(x, dict)]
        return []

    @staticmethod
    def _get_price(item: Dict[str, Any]) -> Optional[float]:
        price = _get_nested(item, [["price"], ["price_value"], ["item", "price"], ["public_price"]])
        try:
            if price is None:
                return None
            return float(price)
        except Exception:
            return None

    @staticmethod
    def _get_title(item: Dict[str, Any]) -> str:
        title = _get_nested(item, [["title"], ["name"], ["item", "title"], ["item", "name"]])
        return str(title or "")

    @staticmethod
    def _get_seller_name(item: Dict[str, Any]) -> str:
        seller = _get_nested(item, [["seller", "username"], ["seller", "name"], ["user", "username"], ["user", "name"]])
        return str(seller or "")

    @staticmethod
    def _get_seller_rating(item: Dict[str, Any]) -> Optional[float]:
        rating = _get_nested(item, [["seller", "rating"], ["seller", "score"], ["seller", "rating_value"], ["user", "rating"]])
        try:
            if rating is None:
                return None
            return float(rating)
        except Exception:
            return None

    @staticmethod
    def _get_seller_reviews(item: Dict[str, Any]) -> Optional[int]:
        cnt = _get_nested(item, [["seller", "reviews_count"], ["seller", "reviews"], ["seller", "reviewsCnt"], ["user", "reviews_count"]])
        try:
            if cnt is None:
                return None
            return int(cnt)
        except Exception:
            return None

    @staticmethod
    def _is_in_stock(item: Dict[str, Any]) -> Optional[bool]:
        # market responses vary; try a few common fields
        v = _get_nested(item, [["in_stock"], ["is_sold"], ["sold"], ["status"]])
        if isinstance(v, bool):
            # some use 'sold' boolean (True means already sold)
            if "sold" in item:
                return not bool(item.get("sold"))
            return v
        if isinstance(v, str):
            # status could be 'active'/'sold'
            if v.lower() in ("sold", "closed", "inactive"):
                return False
            if v.lower() in ("active", "available", "open"):
                return True
        return None

    @staticmethod
    def passes_rules(item: Dict[str, Any], rules: LztSearchRules) -> Tuple[bool, List[str]]:
        reasons: List[str] = []
        title = _lc(LZTMarketClient._get_title(item))
        price = LZTMarketClient._get_price(item)
        seller = _lc(LZTMarketClient._get_seller_name(item))
        rating = LZTMarketClient._get_seller_rating(item)
        reviews = LZTMarketClient._get_seller_reviews(item)
        in_stock = LZTMarketClient._is_in_stock(item)

        # stock
        if rules.require_in_stock and in_stock is False:
            reasons.append("not_in_stock")
            return False, reasons

        # exclude keywords
        for kw in rules.exclude_keywords:
            if kw and _lc(kw) in title:
                reasons.append(f"excluded_keyword:{kw}")
                return False, reasons

        # include keywords (all)
        for kw in rules.include_keywords:
            if kw and _lc(kw) not in title:
                reasons.append(f"missing_keyword:{kw}")
                return False, reasons

        # any keywords
        if rules.any_keywords:
            ok_any = any(_lc(kw) in title for kw in rules.any_keywords if kw)
            if not ok_any:
                reasons.append("missing_any_keyword")
                return False, reasons

        # price
        if price is not None:
            if rules.min_price is not None and price < rules.min_price:
                reasons.append("price_too_low")
                return False, reasons
            if rules.max_price is not None and price > rules.max_price:
                reasons.append("price_too_high")
                return False, reasons
        else:
            # if can't read price and user set bounds, reject
            if rules.min_price is not None or rules.max_price is not None:
                reasons.append("price_unknown")
                return False, reasons

        # seller lists
        if rules.seller_whitelist and seller:
            if seller not in {_lc(x) for x in rules.seller_whitelist}:
                reasons.append("seller_not_whitelisted")
                return False, reasons

        if rules.seller_blacklist and seller:
            if seller in {_lc(x) for x in rules.seller_blacklist}:
                reasons.append("seller_blacklisted")
                return False, reasons

        # rating
        if rules.min_seller_rating is not None:
            if rating is None or rating < rules.min_seller_rating:
                reasons.append("seller_rating_low_or_unknown")
                return False, reasons

        # reviews
        if rules.min_seller_reviews is not None:
            if reviews is None or reviews < rules.min_seller_reviews:
                reasons.append("seller_reviews_low_or_unknown")
                return False, reasons

        return True, reasons

    @staticmethod
    def score_item(item: Dict[str, Any], rules: LztSearchRules) -> float:
        """Higher score = better."""
        score = 0.0
        price = LZTMarketClient._get_price(item)
        rating = LZTMarketClient._get_seller_rating(item)
        reviews = LZTMarketClient._get_seller_reviews(item)
        title = _lc(LZTMarketClient._get_title(item))

        # price preference: cheaper is better within bounds
        if price is not None and rules.max_price:
            # normalized: 0..1
            score += max(0.0, min(1.0, (rules.max_price - price) / max(1.0, rules.max_price)))

        # rating + reviews preference
        if rating is not None:
            score += max(0.0, min(1.0, rating / 5.0)) * 0.8
        if reviews is not None:
            score += max(0.0, min(1.0, reviews / 500.0)) * 0.4

        # keyword bonus
        for kw in rules.include_keywords + rules.any_keywords:
            if kw and _lc(kw) in title:
                score += 0.2

        return score

    def auto_buy(self, cfg: LztAutoBuyConfig) -> Dict[str, Any]:
        """Search several pages, pick the best matching item, and fast-buy it."""
        best: Optional[Dict[str, Any]] = None
        best_score = -1e9
        best_debug: Dict[str, Any] = {}

        # iterate pages
        params = dict(cfg.search_params or {})
        for page in range(1, max(1, cfg.max_pages) + 1):
            params.setdefault("page", page)
            payload = self.list_category(cfg.category, params)
            items = self.extract_items(payload)

            for it in items:
                ok, reasons = self.passes_rules(it, cfg.rules)
                if not ok:
                    continue
                sc = self.score_item(it, cfg.rules)
                if sc > best_score:
                    best_score = sc
                    best = it
                    best_debug = {
                        "page": page,
                        "score": sc,
                        "title": self._get_title(it),
                        "price": self._get_price(it),
                        "seller": self._get_seller_name(it),
                        "matched": True,
                        "reasons": reasons,
                    }

            # pacing between pages
            self._sleep(cfg.per_page_delay_sec)

        if not best:
            return {
                "success": False,
                "message": "No matching item found",
                "debug": {
                    "category": cfg.category,
                    "searched_pages": cfg.max_pages,
                    "search_params": cfg.search_params,
                    "rules": cfg.rules.__dict__,
                }
            }

        item_id = _get_nested(best, [["item_id"], ["id"], ["item", "id"], ["public_id"]]) or best.get("id")
        if not item_id:
            return {"success": False, "message": "Matched item has no id", "debug": best_debug}

        buy_resp = self.fast_buy(item_id, attempts=cfg.buy_attempts, delay_sec=cfg.buy_delay_sec)

        return {
            "success": True,
            "picked": best_debug,
            "item_id": item_id,
            "buy_response": buy_resp,
        }
