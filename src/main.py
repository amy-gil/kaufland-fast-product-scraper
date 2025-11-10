thonimport concurrent.futures
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import requests

# Ensure src directory is on sys.path so namespace packages work
CURRENT_FILE = Path(__file__).resolve()
SRC_DIR = CURRENT_FILE.parent
PROJECT_ROOT = SRC_DIR.parent

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from extractors.kaufland_parser import KauflandParser  # noqa: E402
from outputs.exporter import JSONExporter  # noqa: E402

logger = logging.getLogger("kaufland_fast_product_scraper")

def load_settings() -> Dict[str, Any]:
    settings_path = SRC_DIR / "config" / "settings.json"
    if not settings_path.exists():
        raise FileNotFoundError(f"Config file not found at {settings_path}")
    with settings_path.open("r", encoding="utf-8") as f:
        return json.load(f)

def configure_logging(settings: Dict[str, Any]) -> None:
    log_settings = settings.get("logging", {})
    level_name = log_settings.get("level", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    logger.debug("Logging configured with level %s", level_name)

def load_input_urls() -> List[str]:
    data_dir = PROJECT_ROOT / "data"
    input_file = data_dir / "input_urls.txt"

    if not input_file.exists():
        raise FileNotFoundError(f"Input URLs file not found at {input_file}")

    urls: List[str] = []
    with input_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)

    if not urls:
        raise ValueError("No valid URLs found in input_urls.txt")

    logger.info("Loaded %d URL(s) from %s", len(urls), input_file)
    return urls

def process_single_url(
    url: str,
    parser: KauflandParser,
    sleep_between_requests: float,
) -> List[Dict[str, Any]]:
    logger.info("Processing URL: %s", url)
    try:
        html = parser.fetch_url(url)
        products = parser.parse_products(html, category_url=url)
        logger.info("Parsed %d product(s) from %s", len(products), url)
        return products
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to process URL %s: %s", url, exc)
        return []
    finally:
        if sleep_between_requests > 0:
            time.sleep(sleep_between_requests)

def main() -> None:
    settings = load_settings()
    configure_logging(settings)

    http_settings = settings.get("http", {})
    scraper_settings = settings.get("scraper", {})
    output_settings = settings.get("output", {})

    timeout = http_settings.get("timeout", 15)
    max_retries = http_settings.get("max_retries", 3)
    backoff_factor = http_settings.get("backoff_factor", 0.5)
    user_agent = http_settings.get(
        "user_agent",
        "KauflandFastProductScraper/1.0 (+https://example.com)",
    )

    concurrency = int(scraper_settings.get("concurrency", 5))
    sleep_between_requests = float(scraper_settings.get("sleep_between_requests", 0.1))

    output_directory = output_settings.get("directory", "data")
    filename_prefix = output_settings.get("filename_prefix", "kaufland_products")

    # Configure HTTP session
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        }
    )

    parser = KauflandParser(
        session=session,
        timeout=timeout,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
    )

    urls = load_input_urls()

    all_products: List[Dict[str, Any]] = []

    logger.info(
        "Starting scraping with concurrency=%d, total URLs=%d",
        concurrency,
        len(urls),
    )

    if concurrency <= 1 or len(urls) == 1:
        # Sequential
        for url in urls:
            products = process_single_url(
                url=url,
                parser=parser,
                sleep_between_requests=sleep_between_requests,
            )
            all_products.extend(products)
    else:
        # Parallel with ThreadPoolExecutor
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            future_to_url = {
                executor.submit(
                    process_single_url,
                    url,
                    parser,
                    sleep_between_requests,
                ): url
                for url in urls
            }

            for future in concurrent.futures.as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    products = future.result()
                    all_products.extend(products)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Unhandled error while processing %s: %s", url, exc)

    logger.info("Total products scraped: %d", len(all_products))

    exporter = JSONExporter(
        output_dir=PROJECT_ROOT / output_directory,
    )
    output_path = exporter.export(
        products=all_products,
        filename_prefix=filename_prefix,
    )

    logger.info("Export completed: %s", output_path)

if __name__ == "__main__":
    main()