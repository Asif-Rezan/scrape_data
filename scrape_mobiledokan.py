#!/usr/bin/env python3
"""Scrape phone specifications from MobileDokan into a local MySQL database."""

from __future__ import annotations

import argparse
import hashlib
import logging
import mimetypes
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse
from xml.etree import ElementTree

import mysql.connector
import requests
from bs4 import BeautifulSoup, Tag
from dotenv import load_dotenv
import os


BASE_URL = "https://www.mobiledokan.co"
SITEMAP_INDEX = f"{BASE_URL}/sitemap_index.xml"
USER_AGENT = (
    "MobileDokanResearchScraper/1.0 "
    "(personal data import; contact: local-user)"
)
PHONE_TYPES = {"smartphones", "smartphone", "feature phones", "feature phone"}
XML_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
LOG = logging.getLogger("mobiledokan")


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def norm(value: str) -> str:
    return clean(value).casefold().rstrip(":")


def first_value(specs: dict[tuple[str, str], str], *names: str) -> str | None:
    wanted = {norm(name) for name in names}
    for (_, key), value in specs.items():
        if norm(key) in wanted and value:
            return value
    return None


def parse_price(value: str | None) -> Decimal | None:
    if not value:
        return None
    # Bengali prices on this site are normally rendered with ASCII digits.
    matches = re.findall(r"\d[\d,]*(?:\.\d+)?", value)
    if not matches:
        return None
    try:
        return Decimal(matches[-1].replace(",", ""))
    except Exception:
        return None


def mysql_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        ).replace(tzinfo=None)
    except ValueError:
        return None


@dataclass
class ProductLink:
    url: str
    last_modified: str | None


class MobileDokanScraper:
    def __init__(self, delay: float, timeout: int, image_root: Path):
        self.delay = max(delay, 0.0)
        self.timeout = timeout
        self.image_root = image_root.resolve()
        self.last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.8",
            }
        )

    def get(self, url: str) -> requests.Response:
        host = urlparse(url).hostname
        if host not in {"mobiledokan.co", "www.mobiledokan.co"}:
            raise ValueError(f"Refusing to request an unexpected host: {host}")
        elapsed = time.monotonic() - self.last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        response = self.session.get(url, timeout=self.timeout)
        self.last_request = time.monotonic()
        response.raise_for_status()
        return response

    def product_links(self) -> list[ProductLink]:
        root = ElementTree.fromstring(self.get(SITEMAP_INDEX).content)
        sitemap_urls = [
            clean(node.text)
            for node in root.findall(".//sm:sitemap/sm:loc", XML_NS)
            if "aps-products-sitemap" in clean(node.text)
        ]
        products: dict[str, ProductLink] = {}
        for number, sitemap_url in enumerate(sitemap_urls, 1):
            LOG.info("Reading product sitemap %d/%d", number, len(sitemap_urls))
            sitemap = ElementTree.fromstring(self.get(sitemap_url).content)
            for node in sitemap.findall(".//sm:url", XML_NS):
                loc = node.find("sm:loc", XML_NS)
                modified = node.find("sm:lastmod", XML_NS)
                if loc is None or not clean(loc.text):
                    continue
                url = clean(loc.text)
                products[url] = ProductLink(
                    url=url,
                    last_modified=clean(modified.text) if modified is not None else None,
                )
        return list(products.values())

    def parse_product(
        self, link: ProductLink, include_all_products: bool = False
    ) -> tuple[dict, dict[tuple[str, str], str], list[str]]:
        soup = BeautifulSoup(self.get(link.url).text, "html.parser")
        name_node = soup.select_one("h1")
        if not name_node:
            raise ValueError("Product title was not found")

        specs = self._extract_specs(soup)
        name = clean(name_node.get_text(" ", strip=True))
        brand = first_value(specs, "Brand") or self._breadcrumb_brand(soup)
        device_type = first_value(specs, "Device")

        status = first_value(specs, "Status")
        price_text = self._extract_price(soup, name)
        image_paths = []
        if include_all_products or is_phone(device_type):
            image_paths = self._download_product_images(
                soup,
                brand,
                phone_slug=urlparse(link.url).path.rstrip("/").split("/")[-1],
            )

        phone = {
            "source_url": link.url,
            "slug": urlparse(link.url).path.rstrip("/").split("/")[-1],
            "name": name,
            "brand": brand,
            "model": first_value(specs, "Model"),
            "device_type": device_type,
            "status_text": status,
            "price_text": price_text,
            "price_bdt": parse_price(price_text),
            "image_path": image_paths[0] if image_paths else None,
            "announced": first_value(specs, "Announced"),
            "released": self._released(status, soup),
            "operating_system": first_value(specs, "OS"),
            "chipset": first_value(specs, "Chipset"),
            "ram": first_value(specs, "RAM"),
            "storage": first_value(specs, "Internal", "ROM"),
            "display": self._combined(specs, "Display", "Type", "Size", "Resolution"),
            "main_camera": self._combined(
                specs, "Main camera", "Single", "Dual", "Triple", "Quad", "Features"
            ),
            "selfie_camera": self._combined(
                specs, "Selfie camera", "Single", "Dual", "Features"
            ),
            "battery": self._combined(specs, "Battery", "Type", "Capacity", "Charging"),
            "colors": first_value(specs, "Color", "Colors"),
            "source_last_modified": mysql_datetime(link.last_modified),
            "scraped_at": datetime.now(timezone.utc).replace(tzinfo=None),
        }
        return phone, specs, image_paths

    def _download_product_images(
        self, soup: BeautifulSoup, brand: str | None, phone_slug: str
    ) -> list[str]:
        urls: list[str] = []
        for image in soup.select(
            ".aps-main-image img, .aps-thumb-item img, .aps-product-gallery img"
        ):
            source = clean(
                image.get("data-src")
                or image.get("data-lazy-src")
                or image.get("src")
            )
            if source.startswith("https://www.mobiledokan.co/wp-content/uploads/"):
                urls.append(source)

        if not urls:
            meta = soup.select_one('meta[property="og:image"]')
            source = clean(meta.get("content")) if meta else ""
            if source.startswith("https://www.mobiledokan.co/wp-content/uploads/"):
                urls.append(source)

        urls = list(dict.fromkeys(urls))
        brand_dir = re.sub(r"[^a-z0-9-]+", "-", norm(brand or "unknown")).strip("-")
        destination_dir = (self.image_root / brand_dir / phone_slug).resolve()
        if self.image_root not in destination_dir.parents:
            raise ValueError("Unsafe image destination")
        destination_dir.mkdir(parents=True, exist_ok=True)

        stored: list[str] = []
        for position, url in enumerate(urls, 1):
            response = self.get(url)
            content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
            if content_type and not content_type.startswith("image/"):
                raise ValueError(f"Expected an image but received {content_type}: {url}")
            suffix = Path(urlparse(url).path).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}:
                suffix = mimetypes.guess_extension(content_type) or ".img"
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
            filename = f"{position:02d}-{digest}{suffix}"
            destination = destination_dir / filename
            if not destination.exists() or destination.stat().st_size == 0:
                temporary = destination.with_suffix(destination.suffix + ".part")
                temporary.write_bytes(response.content)
                temporary.replace(destination)
            relative = destination.relative_to(Path(__file__).resolve().parent)
            stored.append(relative.as_posix())
        return stored

    @staticmethod
    def _extract_specs(soup: BeautifulSoup) -> dict[tuple[str, str], str]:
        result: dict[tuple[str, str], str] = {}
        specifications_heading = soup.find(
            lambda tag: tag.name in {"h2", "h3"}
            and norm(tag.get_text(" ", strip=True)) == "specifications"
        )
        scope: Tag = (
            specifications_heading.parent
            if specifications_heading and isinstance(specifications_heading.parent, Tag)
            else soup
        )

        # MobileDokan uses rows with title/value cells; tables are also supported
        # so small theme changes do not require a database migration.
        for row in scope.select("tr"):
            cells = row.find_all(["th", "td"], recursive=False)
            if len(cells) < 2:
                continue
            key = clean(cells[0].get_text(" ", strip=True))
            value = clean(" ".join(c.get_text(" ", strip=True) for c in cells[1:]))
            if key and value:
                section = MobileDokanScraper._nearest_heading(row)
                result[(section, key)] = value

        title_selectors = (
            ".aps-attr-title, .aps-feature-title, .aps-spec-title, "
            ".aps-attr-name, .aps-spec-name"
        )
        for title in scope.select(title_selectors):
            key = clean(title.get_text(" ", strip=True))
            parent = title.parent
            if not key or not isinstance(parent, Tag):
                continue
            value_node = parent.select_one(
                ".aps-attr-value, .aps-feature-value, .aps-spec-value, .aps-attr-data"
            )
            if value_node is None:
                siblings = [x for x in parent.find_all(recursive=False) if x is not title]
                value = clean(" ".join(x.get_text(" ", strip=True) for x in siblings))
            else:
                value = clean(value_node.get_text(" ", strip=True))
            if value:
                result[(MobileDokanScraper._nearest_heading(parent), key)] = value

        # The current theme commonly renders spec rows as divs with two direct
        # children. Limit this fallback to the Specifications area.
        for row in scope.select(".aps-specs-list > li, .aps-specs-list > div, .aps-attrs-list > li"):
            parts = row.find_all(recursive=False)
            if len(parts) < 2:
                continue
            key = clean(parts[0].get_text(" ", strip=True))
            value = clean(" ".join(x.get_text(" ", strip=True) for x in parts[1:]))
            if key and value and len(key) <= 190:
                result[(MobileDokanScraper._nearest_heading(row), key)] = value

        if not result:
            raise ValueError("No specification rows were found; page layout may have changed")
        return result

    @staticmethod
    def _nearest_heading(node: Tag) -> str:
        heading = node.find_previous(["h2", "h3", "h4"])
        value = clean(heading.get_text(" ", strip=True)) if heading else "General"
        return value[:120] or "General"

    @staticmethod
    def _combined(
        specs: dict[tuple[str, str], str], section_name: str, *keys: str
    ) -> str | None:
        wanted_section = norm(section_name)
        wanted_keys = {norm(key) for key in keys}
        values = [
            value
            for (section, key), value in specs.items()
            if wanted_section in norm(section) and norm(key) in wanted_keys
        ]
        return " | ".join(dict.fromkeys(values)) or None

    @staticmethod
    def _breadcrumb_brand(soup: BeautifulSoup) -> str | None:
        for anchor in soup.select(
            '.breadcrumb a[href*="/brand/"], .breadcrumbs a[href*="/brand/"], '
            'a[href*="/brand/"]'
        ):
            value = clean(anchor.get_text(" ", strip=True))
            if value:
                return value
        return None

    @staticmethod
    def _extract_price(soup: BeautifulSoup, product_name: str) -> str | None:
        # Prefer price elements close to the product header.
        for selector in (
            ".aps-feature-price",
            ".aps-product-price",
            ".aps-price-value",
            ".aps-price",
            '[class*="product-price"]',
        ):
            node = soup.select_one(selector)
            if node:
                value = clean(node.get_text(" ", strip=True))
                if value:
                    return value
        text = clean(soup.get_text(" ", strip=True))
        match = re.search(
            rf"(?:Price\s+in\s+Bangladesh.*?|{re.escape(product_name)}.*?)"
            r"(?:BDT\.?|৳)\s*([\d,]+)",
            text,
            re.IGNORECASE,
        )
        return f"BDT {match.group(1)}" if match else None

    @staticmethod
    def _released(status: str | None, soup: BeautifulSoup) -> str | None:
        if status:
            match = re.search(r"(?:released|release)\s+(.+)$", status, re.IGNORECASE)
            if match:
                return clean(match.group(1))
        text = clean(soup.get_text(" ", strip=True))
        match = re.search(r"Released\s+(.{4,40}?)(?:\s{2}| OS | Display )", text)
        return clean(match.group(1)) if match else None


class Database:
    def __init__(self):
        self.connection = mysql.connector.connect(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_PORT", "3306")),
            user=os.getenv("MYSQL_USER", "root"),
            password=os.getenv("MYSQL_PASSWORD", ""),
            database=os.getenv("MYSQL_DATABASE", "product_data"),
            charset="utf8mb4",
            autocommit=False,
        )

    def close(self) -> None:
        self.connection.close()

    def apply_schema(self) -> None:
        schema_path = Path(__file__).with_name("schema.sql")
        cursor = self.connection.cursor()
        statement = ""
        try:
            for line in schema_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("--"):
                    continue
                statement += line + "\n"
                if stripped.endswith(";"):
                    sql = statement.strip().rstrip(";")
                    # Already connected to the selected DB, so USE/CREATE are unnecessary.
                    if not sql.upper().startswith(("CREATE DATABASE", "USE ")):
                        cursor.execute(sql)
                    statement = ""
            self.connection.commit()
            # Upgrade databases created by an earlier version of this scraper.
            cursor.execute(
                "ALTER TABLE phones ADD COLUMN IF NOT EXISTS "
                "image_path VARCHAR(1000) NULL AFTER price_bdt"
            )
            cursor.execute("ALTER TABLE phones DROP COLUMN IF EXISTS image_url")
            self.connection.commit()
        finally:
            cursor.close()

    def upsert(
        self,
        phone: dict,
        specs: dict[tuple[str, str], str],
        image_paths: list[str],
    ) -> None:
        columns = list(phone)
        placeholders = ", ".join(["%s"] * len(columns))
        updates = ", ".join(
            f"`{column}` = VALUES(`{column}`)"
            for column in columns
            if column not in {"source_url", "scraped_at"}
        )
        sql = (
            f"INSERT INTO phones ({', '.join(f'`{c}`' for c in columns)}) "
            f"VALUES ({placeholders}) ON DUPLICATE KEY UPDATE "
            f"{updates}, scraped_at = VALUES(scraped_at), id = LAST_INSERT_ID(id)"
        )
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql, [phone[column] for column in columns])
            phone_id = cursor.lastrowid
            cursor.execute("DELETE FROM phone_specs WHERE phone_id = %s", (phone_id,))
            rows = [
                (phone_id, section[:120], key[:190], value)
                for (section, key), value in specs.items()
            ]
            cursor.executemany(
                """
                INSERT INTO phone_specs
                    (phone_id, section_name, spec_name, spec_value)
                VALUES (%s, %s, %s, %s)
                """,
                rows,
            )
            cursor.execute("DELETE FROM phone_images WHERE phone_id = %s", (phone_id,))
            cursor.executemany(
                """
                INSERT INTO phone_images
                    (phone_id, image_path, sort_order, is_primary)
                VALUES (%s, %s, %s, %s)
                """,
                [
                    (phone_id, path, position, position == 1)
                    for position, path in enumerate(image_paths, 1)
                ],
            )
            cursor.execute(
                "DELETE FROM scrape_failures WHERE source_url = %s", (phone["source_url"],)
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            cursor.close()

    def record_failure(self, url: str, message: str) -> None:
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO scrape_failures
                    (source_url, error_message, attempts, last_attempt_at)
                VALUES (%s, %s, 1, UTC_TIMESTAMP())
                ON DUPLICATE KEY UPDATE
                    error_message = VALUES(error_message),
                    attempts = attempts + 1,
                    last_attempt_at = VALUES(last_attempt_at)
                """,
                (url, message[:65000]),
            )
            self.connection.commit()
        finally:
            cursor.close()


def is_phone(device_type: str | None) -> bool:
    return norm(device_type or "") in PHONE_TYPES


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape MobileDokan smartphones and feature phones into MySQL."
    )
    parser.add_argument("--limit", type=int, help="Only process N product pages (for testing).")
    parser.add_argument(
        "--all-products",
        action="store_true",
        help="Also store tablets and wearables; default is phones only.",
    )
    parser.add_argument(
        "--url",
        help="Process one product URL instead of reading all product sitemaps.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=float(os.getenv("SCRAPE_DELAY_SECONDS", "1.5")),
        help="Seconds between HTTP requests (default: 1.5).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    scraper = MobileDokanScraper(
        delay=args.delay,
        timeout=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")),
        image_root=Path(__file__).resolve().parent / "images",
    )
    database = Database()
    stored = skipped = failed = 0
    try:
        database.apply_schema()
        links: Iterable[ProductLink]
        if args.url:
            links = [ProductLink(args.url, None)]
        else:
            links = scraper.product_links()
        links = list(links)
        if args.limit is not None:
            links = links[: max(args.limit, 0)]
        LOG.info("Found %d product page(s) to process", len(links))

        for index, link in enumerate(links, 1):
            try:
                phone, specs, image_paths = scraper.parse_product(
                    link, include_all_products=args.all_products
                )
                if not args.all_products and not is_phone(phone["device_type"]):
                    skipped += 1
                    LOG.info(
                        "[%d/%d] Skipped non-phone: %s (%s)",
                        index, len(links), phone["name"], phone["device_type"],
                    )
                    continue
                database.upsert(phone, specs, image_paths)
                stored += 1
                LOG.info("[%d/%d] Stored: %s", index, len(links), phone["name"])
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                failed += 1
                LOG.exception("[%d/%d] Failed: %s", index, len(links), link.url)
                database.record_failure(link.url, f"{type(exc).__name__}: {exc}")
    finally:
        database.close()

    LOG.info("Finished. Stored=%d skipped=%d failed=%d", stored, skipped, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
