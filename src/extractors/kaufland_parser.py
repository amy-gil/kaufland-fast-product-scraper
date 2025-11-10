thonimport logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .utils_cleaner import (
    normalize_whitespace,
    parse_float,
    parse_int,
    parse_price,
)

logger = logging.getLogger(__name__)

class KauflandParser:
    """
    Fetch and parse Kaufland search listing pages.

    The parser is designed to be resilient to minor markup changes by using
    several CSS selectors and fallbacks when extracting data.
    """

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        timeout: int = 15,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

    def fetch_url(self, url: str) -> str:
        """Fetch page content with retry and backoff."""
        last_exception: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                logger.debug("Fetching URL (attempt %d/%d): %s", attempt, self.max_retries, url)
                response = self.session.get(url, timeout=self.timeout)
                response.raise_for_status()
                logger.info("Fetched %s (%d bytes)", url, len(response.text))
                return response.text
            except requests.RequestException as exc:  # noqa: PERF203
                last_exception = exc
                wait = self.backoff_factor * attempt
                logger.warning(
                    "Request failed for %s (attempt %d/%d): %s; retrying in %.1fs",
                    url,
                    attempt,
                    self.max_retries,
                    exc,
                    wait,
                )
                time.sleep(wait)

        msg = f"Failed to fetch {url} after {self.max_retries} attempts"
        logger.error(msg)
        if last_exception:
            raise RuntimeError(msg) from last_exception
        raise RuntimeError(msg)

    def parse_products(self, html: str, category_url: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Parse product data from a Kaufland search results HTML page.

        The scraper attempts multiple patterns that are commonly found on
        e-commerce listing pages; if a piece of data can't be extracted,
        it is set to None or a sensible default.
        """
        soup = BeautifulSoup(html, "html.parser")

        # Product containers: try various selectors
        product_nodes = (
            soup.select('[data-test="product-tile"]')
            or soup.select("article[data-product-id]")
            or soup.select("[data-product-id]")
        )

        if not product_nodes:
            logger.warning("No product containers found in page")
            return []

        products: List[Dict[str, Any]] = []
        for node in product_nodes:
            try:
                product = self._parse_single_product(node, category_url)
                if product:
                    products.append(product)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to parse product node: %s", exc)

        return products

    def _parse_single_product(self, node: Any, category_url: Optional[str]) -> Optional[Dict[str, Any]]:
        # ID and EAN from data attributes, if available
        raw_id = (
            node.get("data-product-id")
            or node.get("data-id")
            or node.get("data-productid")
        )
        product_id = parse_int(raw_id) if raw_id else None

        ean = (
            node.get("data-ean")
            or node.get("data-product-ean")
        )

        # Title
        title_el = (
            node.select_one('[data-test="product-title"]')
            or node.select_one(".product__title")
            or node.select_one("a[title]")
            or node.select_one("a")
        )
        title = normalize_whitespace(title_el.get_text(strip=True)) if title_el else ""

        # Prices
        price_el = (
            node.select_one('[data-test="product-price"]')
            or node.select_one(".product__price")
            or node.select_one(".product-price")
        )
        original_price_el = (
            node.select_one(".product__price--strikethrough")
            or node.select_one(".product-price__strikethrough")
        )

        price = parse_price(price_el.get_text(" ", strip=True)) if price_el else None
        original_price = (
            parse_price(original_price_el.get_text(" ", strip=True))
            if original_price_el
            else None
        )

        # Rating
        rating_container = (
            node.select_one('[data-test="product-rating"]')
            or node.select_one(".rating")
            or node.select_one(".product-rating")
        )
        rating_average: Optional[float] = None
        rating_count: Optional[int] = None

        if rating_container:
            # Try data attributes
            raw_avg = rating_container.get("data-rating") or rating_container.get("data-average")
            raw_count = rating_container.get("data-count") or rating_container.get("data-rating-count")

            if raw_avg:
                rating_average = parse_float(raw_avg)
            if raw_count:
                rating_count = parse_int(raw_count)

            # Fallback: text-based pattern like "4.5 (17)"
            if rating_average is None or rating_count is None:
                text = rating_container.get_text(" ", strip=True)
                # Extract first float
                if rating_average is None:
                    rating_average = parse_float(text)
                # Extract number in parentheses
                if rating_count is None:
                    rating_count = parse_int(text)

        # Product link
        link_el = title_el if title_el and title_el.name == "a" else node.select_one("a[href]")
        href: Optional[str] = None
        if link_el and link_el.has_attr("href"):
            href = link_el["href"]

        full_link: Optional[str] = None
        if href:
            full_link = urljoin(category_url or "", href)

        # Images
        images: List[str] = []
        for img in node.select("img[src]"):
            src = img.get("src")
            if src:
                images.append(src)

        # If nothing meaningful found, skip
        if not title and product_id is None and not full_link:
            logger.debug("Skipping node with no identifiable product data")
            return None

        product: Dict[str, Any] = {
            "id": product_id,
            "ean": ean,
            "title": title,
            "rating": {
                "average": rating_average,
                "count": rating_count,
            },
            "price": price,
            "originalPrice": original_price,
            "link": full_link,
            "categoryUrl": category_url,
            "images": images,
        }

        logger.debug("Parsed product: %r", product)
        return product