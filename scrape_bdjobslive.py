#!/usr/bin/env python3
"""Import all currently active BDJobsLive jobs into local MySQL."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import mysql.connector
import requests
from bs4 import BeautifulSoup, Tag
from dotenv import load_dotenv


SITE = "https://www.bdjobslive.com"
API = "https://admin.bdjobslive.com/api"
LIST_ENDPOINT = f"{API}/get-jobs"
USER_AGENT = "BDJobsLiveResearchImporter/1.0 (personal local database import)"
ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("bdjobslive")


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_datetime(value) -> datetime | None:
    value = clean(value)
    if not value:
        return None
    for candidate in (value, value.replace("Z", "+00:00")):
        try:
            result = datetime.fromisoformat(candidate)
            if result.tzinfo:
                result = result.astimezone(timezone.utc).replace(tzinfo=None)
            return result
        except ValueError:
            pass
    for pattern in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            pass
    return None


class Client:
    def __init__(self, delay: float, timeout: int):
        self.delay = max(delay, 0)
        self.timeout = timeout
        self.last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": USER_AGENT, "Accept": "application/json,text/html;q=0.9,*/*;q=0.8"}
        )

    def get(self, url: str, **kwargs) -> requests.Response:
        host = (urlparse(url).hostname or "").lower()
        if host not in {"www.bdjobslive.com", "bdjobslive.com", "admin.bdjobslive.com"}:
            raise ValueError(f"Unexpected host refused: {host}")
        remaining = self.delay - (time.monotonic() - self.last_request)
        if remaining > 0:
            time.sleep(remaining)
        response = self.session.get(url, timeout=self.timeout, **kwargs)
        self.last_request = time.monotonic()
        response.raise_for_status()
        return response

    def all_listings(self) -> list[dict]:
        listings: dict[int, dict] = {}
        page = 1
        while True:
            payload = self.get(
                LIST_ENDPOINT, params={"page": page, "per_page": 200}
            ).json()
            if not payload.get("status"):
                raise RuntimeError(payload.get("message", "Listing API returned failure"))
            paginator = payload["data"]["jobs"]
            for item in paginator.get("data", []):
                listings[int(item["id"])] = item
            last_page = int(paginator.get("last_page") or 1)
            LOG.info("Read listing page %d/%d", page, last_page)
            if page >= last_page:
                break
            page += 1
        return list(listings.values())

    def detail(
        self, listing: dict, download_logo: bool = True
    ) -> tuple[dict, list[tuple[str, str]]]:
        source_url = f"{SITE}/bdjobs-details/{listing['slug']}"
        soup = BeautifulSoup(self.get(source_url).text, "html.parser")
        heading = soup.find("h1")
        if not heading:
            LOG.warning(
                "Detail page is unavailable; storing listing data only: %s",
                source_url,
            )
            return self._listing_only(listing, source_url, download_logo), []
        card = self._job_card(heading)
        card_text = clean(card.get_text(" ", strip=True))
        sections = self._sections(card)

        labels = {}
        for key in (
            "Vacancy", "Age", "Location", "Salary", "Experience", "Gender",
            "Job Type", "Industry", "Published",
        ):
            match = re.search(
                rf"(?:^|\s){re.escape(key)}\s*:\s*(.*?)(?=\s(?:Vacancy|Age|Location|Salary|Experience|Gender|Job Type|Industry|Published)\s*:|$)",
                card_text,
                re.IGNORECASE,
            )
            if match:
                labels[key] = clean(match.group(1))

        deadline_match = re.search(
            r"Application Deadline\s*:\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
            card_text,
            re.IGNORECASE,
        )
        salary_text = labels.get("Salary")
        data = {
            "source_job_id": int(listing["id"]),
            "source_url": source_url,
            "slug": listing["slug"],
            "title": clean(heading.get_text(" ", strip=True)),
            "company_id": listing.get("company_id"),
            "company_name": listing.get("company_name"),
            "company_slug": listing.get("company_slug"),
            "company_logo_path": self.download_logo(listing) if download_logo else None,
            "functional_area": listing.get("functional_area"),
            "industry": labels.get("Industry"),
            "job_type": listing.get("job_type") or labels.get("Job Type"),
            "job_shift": listing.get("job_shift"),
            "workplace": self._section(sections, "Workplace"),
            "locations": ", ".join(listing.get("cities") or []) or labels.get("Location"),
            "vacancy": labels.get("Vacancy"),
            "age_requirement": labels.get("Age"),
            "gender_preference": labels.get("Gender"),
            "experience_summary": listing.get("job_experience") or labels.get("Experience"),
            "salary_from": listing.get("salary_from"),
            "salary_to": listing.get("salary_to"),
            "salary_text": salary_text,
            "salary_negotiable": bool(listing.get("is_salary_negotiable")),
            "is_featured": bool(listing.get("is_featured")),
            "education_summary": " | ".join(listing.get("degree_levels") or [])
                or self._section(sections, "Education"),
            "published_at": parse_datetime(listing.get("created_at") or labels.get("Published")),
            "application_deadline": parse_datetime(
                listing.get("expiry_date")
                or (deadline_match.group(1) if deadline_match else None)
            ),
            "full_text": card_text,
            "raw_listing_json": json.dumps(listing, ensure_ascii=False),
            "scraped_at": datetime.now(timezone.utc).replace(tzinfo=None),
        }
        return data, sections

    def _listing_only(
        self, listing: dict, source_url: str, download_logo: bool = True
    ) -> dict:
        return {
            "source_job_id": int(listing["id"]),
            "source_url": source_url,
            "slug": listing["slug"],
            "title": clean(listing.get("title")) or f"Job {listing['id']}",
            "company_id": listing.get("company_id"),
            "company_name": listing.get("company_name"),
            "company_slug": listing.get("company_slug"),
            "company_logo_path": self.download_logo(listing) if download_logo else None,
            "functional_area": listing.get("functional_area"),
            "industry": None,
            "job_type": listing.get("job_type"),
            "job_shift": listing.get("job_shift"),
            "workplace": None,
            "locations": ", ".join(listing.get("cities") or []),
            "vacancy": None,
            "age_requirement": None,
            "gender_preference": None,
            "experience_summary": listing.get("job_experience"),
            "salary_from": listing.get("salary_from"),
            "salary_to": listing.get("salary_to"),
            "salary_text": None,
            "salary_negotiable": bool(listing.get("is_salary_negotiable")),
            "is_featured": bool(listing.get("is_featured")),
            "education_summary": " | ".join(listing.get("degree_levels") or []),
            "published_at": parse_datetime(listing.get("created_at")),
            "application_deadline": parse_datetime(listing.get("expiry_date")),
            "full_text": None,
            "raw_listing_json": json.dumps(listing, ensure_ascii=False),
            "scraped_at": datetime.now(timezone.utc).replace(tzinfo=None),
        }

    @staticmethod
    def _job_card(heading: Tag) -> Tag:
        node = heading
        for _ in range(10):
            if not isinstance(node.parent, Tag):
                break
            node = node.parent
            classes = set(node.get("class") or [])
            if "max-w-4xl" in classes:
                return node
        return heading.parent if isinstance(heading.parent, Tag) else heading

    @staticmethod
    def _sections(card: Tag) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        seen: set[str] = set()
        for heading in card.find_all(["h3"]):
            name = clean(heading.get_text(" ", strip=True))
            parent = heading.parent
            if not name or name in seen or not isinstance(parent, Tag):
                continue
            # Nested headings belong to their nearest top-level section.
            if parent.find_parent("div", class_="scroll-mt-24") is not None and "scroll-mt-24" not in (parent.get("class") or []):
                continue
            text = clean(parent.get_text(" ", strip=True))
            if text.casefold().startswith(name.casefold()):
                text = clean(text[len(name):])
            if text:
                result.append((name[:255], text))
                seen.add(name)
        return result

    @staticmethod
    def _section(sections: list[tuple[str, str]], name: str) -> str | None:
        for section, text in sections:
            if section.casefold() == name.casefold():
                return text
        return None

    def download_logo(self, listing: dict) -> str | None:
        url = clean(listing.get("company_logo_url"))
        if not url:
            return None
        suffix = Path(urlparse(url).path).suffix.lower()
        company = str(listing.get("company_id") or "unknown")
        directory = ROOT / "job_images" / "company_logos" / company
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(url.encode()).hexdigest()[:10]
        if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}:
            existing = directory / f"logo-{digest}{suffix}"
            if existing.exists() and existing.stat().st_size > 0:
                return existing.relative_to(ROOT).as_posix()

        response = self.get(url)
        content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
        if content_type and not content_type.startswith("image/"):
            raise ValueError(f"Company logo is not an image: {url}")
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}:
            suffix = mimetypes.guess_extension(content_type) or ".img"
        destination = directory / f"logo-{digest}{suffix}"
        if not destination.exists() or destination.stat().st_size == 0:
            temporary = destination.with_suffix(destination.suffix + ".part")
            temporary.write_bytes(response.content)
            temporary.replace(destination)
        return destination.relative_to(ROOT).as_posix()


class Database:
    def __init__(self):
        self.connection = mysql.connector.connect(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_PORT", "3306")),
            user=os.getenv("MYSQL_USER", "root"),
            password=os.getenv("MYSQL_PASSWORD", ""),
            database=os.getenv("JOBS_MYSQL_DATABASE", "jobs_data"),
            charset="utf8mb4",
            autocommit=False,
        )

    def close(self):
        self.connection.close()

    def apply_schema(self):
        cursor = self.connection.cursor()
        statement = ""
        try:
            for line in (ROOT / "schema_jobs.sql").read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("--"):
                    continue
                statement += line + "\n"
                if stripped.endswith(";"):
                    sql = statement.strip().rstrip(";")
                    if not sql.upper().startswith("USE "):
                        cursor.execute(sql)
                    statement = ""
            self.connection.commit()
        finally:
            cursor.close()

    def upsert(self, data: dict, sections: list[tuple[str, str]]):
        columns = list(data)
        updates = ", ".join(
            f"`{column}`=VALUES(`{column}`)"
            for column in columns
            if column not in {"source_job_id"}
        )
        sql = (
            f"INSERT INTO jobs ({','.join(f'`{x}`' for x in columns)}) "
            f"VALUES ({','.join(['%s'] * len(columns))}) "
            f"ON DUPLICATE KEY UPDATE {updates}, id=LAST_INSERT_ID(id)"
        )
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql, [data[x] for x in columns])
            job_id = cursor.lastrowid
            cursor.execute("DELETE FROM job_sections WHERE job_id=%s", (job_id,))
            cursor.executemany(
                "INSERT INTO job_sections(job_id,section_name,section_text,sort_order) "
                "VALUES(%s,%s,%s,%s)",
                [(job_id, name, text, order) for order, (name, text) in enumerate(sections, 1)],
            )
            cursor.execute(
                "DELETE FROM job_scrape_failures WHERE source_url=%s",
                (data["source_url"],),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            cursor.close()

    def failure(self, url: str, error: str):
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO job_scrape_failures
                    (source_url,error_message,attempts,last_attempt_at)
                VALUES(%s,%s,1,UTC_TIMESTAMP())
                ON DUPLICATE KEY UPDATE error_message=VALUES(error_message),
                    attempts=attempts+1,last_attempt_at=VALUES(last_attempt_at)
                """,
                (url, error[:65000]),
            )
            self.connection.commit()
        finally:
            cursor.close()


def arguments():
    parser = argparse.ArgumentParser(description="Import active BDJobsLive jobs.")
    parser.add_argument("--limit", type=int, help="Process only N jobs for testing.")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between requests.")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = arguments()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    client = Client(args.delay, int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")))
    database = Database()
    stored = failed = 0
    try:
        database.apply_schema()
        listings = client.all_listings()
        if args.limit is not None:
            listings = listings[:max(args.limit, 0)]
        LOG.info("Processing %d active jobs", len(listings))
        for index, listing in enumerate(listings, 1):
            url = f"{SITE}/bdjobs-details/{listing['slug']}"
            try:
                data, sections = client.detail(listing)
                database.upsert(data, sections)
                stored += 1
                LOG.info("[%d/%d] Stored: %s", index, len(listings), data["title"])
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                failed += 1
                LOG.exception("[%d/%d] Failed: %s", index, len(listings), url)
                database.failure(url, f"{type(exc).__name__}: {exc}")
    finally:
        database.close()
    LOG.info("Finished. Stored=%d failed=%d", stored, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
