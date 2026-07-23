#!/usr/bin/env python3
"""Publish attributed r/jobs RSS entries to the Chakrie timeline API."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

import mysql.connector
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


load_dotenv()

REDDIT_RSS_URL = "https://www.reddit.com/r/jobs/.rss"
CHAKRIE_API_TOKEN = os.getenv("CHAKRIE_API_TOKEN", "")
CHAKRIE_TIMELINE_API_URL = os.getenv(
    "CHAKRIE_TIMELINE_API_URL",
    "https://www.chakrie.com/api/v1/mobile/timeline/posts/create",
)
ROOT = Path(__file__).resolve().parent
MEDIA_ROOT = ROOT / "timeline_media" / "reddit_jobs"
CACHE_PATH = ROOT / "cache" / "reddit_jobs.rss"
ATOM = {"a": "http://www.w3.org/2005/Atom"}
MEDIA = {"m": "http://search.yahoo.com/mrss/"}
LOG = logging.getLogger("reddit-timeline")


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


@dataclass
class RedditPost:
    post_id: str
    title: str
    author: str
    text: str
    url: str
    image_url: str | None

    def timeline_body(self, max_length: int = 1800) -> str:
        attribution = f"\n\nPosted by {self.author} on r/jobs\nSource: {self.url}"
        header = self.title
        remaining = max(max_length - len(header) - len(attribution) - 2, 0)
        excerpt = self.text[:remaining].rstrip()
        if len(self.text) > remaining:
            excerpt = excerpt.rstrip(" .") + "…"
        return f"{header}\n\n{excerpt}{attribution}" if excerpt else f"{header}{attribution}"


class Reddit:
    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "ChakrieTimelineRSSImporter/1.0",
                "Accept": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )

    def posts(self) -> list[RedditPost]:
        content = self._rss_content()
        root = ElementTree.fromstring(content)
        result: list[RedditPost] = []
        for entry in root.findall("a:entry", ATOM):
            post_id = clean(entry.findtext("a:id", namespaces=ATOM))
            title = clean(entry.findtext("a:title", namespaces=ATOM))
            author = clean(entry.findtext("a:author/a:name", namespaces=ATOM))
            link = entry.find("a:link", ATOM)
            url = clean(link.get("href")) if link is not None else ""
            content = entry.findtext("a:content", default="", namespaces=ATOM)
            parsed = BeautifulSoup(content, "html.parser")
            text = clean(" ".join(p.get_text(" ", strip=True) for p in parsed.select(".md p")))
            if not text:
                text = clean(parsed.get_text(" ", strip=True).split("submitted by")[0])
            image_url = self._image(entry, parsed)
            if (
                post_id
                and title
                and url
                and author.casefold() != "/u/automoderator"
            ):
                result.append(RedditPost(post_id, title, author, text, url, image_url))
        return result

    def _rss_content(self) -> bytes:
        response = None
        for attempt, delay in enumerate((0, 2, 5, 10), 1):
            if delay:
                LOG.warning(
                    "Reddit rate limit encountered; retrying in %d seconds "
                    "(attempt %d/4)",
                    delay,
                    attempt,
                )
                time.sleep(delay)
            response = self.session.get(REDDIT_RSS_URL, timeout=self.timeout)
            if response.status_code != 429:
                response.raise_for_status()
                CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                CACHE_PATH.write_bytes(response.content)
                return response.content

        if CACHE_PATH.exists() and CACHE_PATH.stat().st_size > 0:
            LOG.warning(
                "Reddit still returned HTTP 429; using the last cached RSS feed."
            )
            return CACHE_PATH.read_bytes()
        retry_after = clean(response.headers.get("Retry-After")) if response else ""
        suffix = f" Retry after {retry_after} seconds." if retry_after else ""
        raise RuntimeError(
            "Reddit temporarily rate-limited RSS requests (HTTP 429)."
            f"{suffix} Wait a few minutes, then run the command again."
        )

    @staticmethod
    def _image(entry: ElementTree.Element, parsed: BeautifulSoup) -> str | None:
        # Prefer an original i.redd.it image over a resized preview.
        for anchor in parsed.select("a[href]"):
            value = clean(anchor.get("href"))
            if (urlparse(value).hostname or "").lower() == "i.redd.it":
                return value
        thumbnail = entry.find("m:thumbnail", MEDIA)
        if thumbnail is not None:
            return clean(thumbnail.get("url")) or None
        image = parsed.select_one("img[src]")
        return clean(image.get("src")) if image else None

    def download(self, post: RedditPost) -> Path | None:
        if not post.image_url:
            return None
        host = (urlparse(post.image_url).hostname or "").lower()
        allowed = {"i.redd.it", "preview.redd.it", "external-preview.redd.it"}
        if host not in allowed:
            raise ValueError(f"Unexpected Reddit image host: {host}")
        response = self.session.get(post.image_url, timeout=self.timeout)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
        if content_type and not content_type.startswith("image/"):
            raise ValueError(f"Reddit media is not an image: {content_type}")
        suffix = Path(urlparse(post.image_url).path).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            suffix = mimetypes.guess_extension(content_type) or ".jpg"
        safe_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", post.post_id)
        digest = hashlib.sha1(post.image_url.encode()).hexdigest()[:10]
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        destination = MEDIA_ROOT / f"{safe_id}-{digest}{suffix}"
        if not destination.exists() or destination.stat().st_size == 0:
            temporary = destination.with_suffix(destination.suffix + ".part")
            temporary.write_bytes(response.content)
            temporary.replace(destination)
        return destination


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
        self.ensure_table()

    def close(self):
        self.connection.close()

    def ensure_table(self):
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS chakrie_timeline_posts (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    reddit_post_id VARCHAR(100) NOT NULL,
                    reddit_url VARCHAR(1000) NOT NULL,
                    chakrie_post_id BIGINT UNSIGNED NULL,
                    body_hash CHAR(64) NOT NULL,
                    media_path VARCHAR(1000) NULL,
                    status VARCHAR(40) NOT NULL,
                    response_json JSON NULL,
                    error_message TEXT NULL,
                    posted_at DATETIME NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    UNIQUE KEY uq_timeline_reddit_id (reddit_post_id),
                    KEY idx_timeline_remote_id (chakrie_post_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                  COLLATE=utf8mb4_unicode_ci
                """
            )
            self.connection.commit()
        finally:
            cursor.close()

    def posted_ids(self) -> set[str]:
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                "SELECT reddit_post_id FROM chakrie_timeline_posts WHERE status='posted'"
            )
            return {row[0] for row in cursor}
        finally:
            cursor.close()

    def record(
        self,
        post: RedditPost,
        body_hash: str,
        media_path: str | None,
        status: str,
        response: dict | None,
        remote_id: int | None = None,
        error: str | None = None,
    ):
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO chakrie_timeline_posts
                    (reddit_post_id,reddit_url,chakrie_post_id,body_hash,
                     media_path,status,response_json,error_message,posted_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,
                       IF(%s='posted',UTC_TIMESTAMP(),NULL))
                ON DUPLICATE KEY UPDATE
                    reddit_url=VALUES(reddit_url),
                    chakrie_post_id=COALESCE(
                        VALUES(chakrie_post_id),chakrie_post_id
                    ),
                    body_hash=VALUES(body_hash),
                    media_path=VALUES(media_path),
                    status=VALUES(status),
                    response_json=VALUES(response_json),
                    error_message=VALUES(error_message),
                    posted_at=IF(
                        VALUES(status)='posted',UTC_TIMESTAMP(),posted_at
                    )
                """,
                (
                    post.post_id,
                    post.url,
                    remote_id,
                    body_hash,
                    media_path,
                    status,
                    json.dumps(response, ensure_ascii=False) if response else None,
                    error,
                    status,
                ),
            )
            self.connection.commit()
        finally:
            cursor.close()


class Chakrie:
    def __init__(self):
        if not CHAKRIE_API_TOKEN:
            raise RuntimeError("CHAKRIE_API_TOKEN is missing from .env")
        self.headers = {
            "Authorization": f"Bearer {CHAKRIE_API_TOKEN}",
            "Accept": "application/json",
            "User-Agent": "LocalTimelinePublisher/1.0",
        }

    def post(self, body: str, media_path: Path) -> dict:
        mime = mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
        with media_path.open("rb") as media:
            response = requests.post(
                CHAKRIE_TIMELINE_API_URL,
                headers=self.headers,
                data={"body": body, "visibility": "public"},
                files={"media": (media_path.name, media, mime)},
                timeout=60,
            )
        try:
            result = response.json()
        except ValueError:
            result = {"raw_response": response.text[:5000]}
        if not response.ok:
            raise requests.HTTPError(
                f"HTTP {response.status_code}: {json.dumps(result, ensure_ascii=False)}",
                response=response,
            )
        return result


def remote_id(response: dict) -> int | None:
    candidates = [
        response.get("id"),
        (response.get("data") or {}).get("id")
        if isinstance(response.get("data"), dict)
        else None,
        (response.get("post") or {}).get("id")
        if isinstance(response.get("post"), dict)
        else None,
    ]
    for value in candidates:
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            pass
    return None


def arguments():
    parser = argparse.ArgumentParser(
        description="Publish attributed r/jobs RSS entries to Chakrie timeline."
    )
    parser.add_argument("--post-id", help="Publish/preview one Reddit ID, e.g. t3_abc.")
    parser.add_argument("--limit", type=int, default=1, help="Number to preview/post.")
    parser.add_argument("--send", action="store_true", help="Actually create timeline posts.")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Required with --send unless --post-id is provided.",
    )
    parser.add_argument("--force", action="store_true", help="Repost tracked Reddit IDs.")
    return parser.parse_args()


def main() -> int:
    args = arguments()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    if args.send and not args.post_id and not args.all:
        raise SystemExit("Refusing bulk publishing: add --all or select --post-id.")

    reddit = Reddit(int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")))
    database = Database()
    failures = 0
    try:
        try:
            posts = [post for post in reddit.posts() if post.image_url]
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from None
        if args.post_id:
            posts = [post for post in posts if post.post_id == args.post_id]
        posted = database.posted_ids()
        if not args.force:
            posts = [post for post in posts if post.post_id not in posted]
        posts = posts[: max(args.limit, 0)]
        if not posts:
            raise SystemExit("No matching unposted RSS entry with an image was found.")
        LOG.info("Selected %d Reddit post(s) with media", len(posts))
        client = Chakrie() if args.send else None
        for post in posts:
            body = post.timeline_body()
            digest = hashlib.sha256(body.encode()).hexdigest()
            media_path = reddit.download(post)
            relative = media_path.relative_to(ROOT).as_posix() if media_path else None
            if not args.send:
                print(
                    json.dumps(
                        {
                            "reddit_post_id": post.post_id,
                            "body": body,
                            "visibility": "public",
                            "media": relative,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                continue
            try:
                result = client.post(body, media_path)
                post_id = remote_id(result)
                database.record(
                    post, digest, relative, "posted", result, remote_id=post_id
                )
                LOG.info("Posted %s (Chakrie ID: %s)", post.post_id, post_id)
            except Exception as exc:
                failures += 1
                database.record(
                    post, digest, relative, "failed", None, error=str(exc)
                )
                LOG.error("Failed %s: %s", post.post_id, exc)
    finally:
        database.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
