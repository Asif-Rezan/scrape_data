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

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image


load_dotenv()

# Change these two values (or set them in .env) to select the Reddit source.
# A normal subreddit URL is enough; the script builds the RSS URL itself.
REDDIT_URL = os.getenv("REDDIT_URL", "https://www.reddit.com/r/jobhunting/")
REDDIT_FETCH_LIMIT = int(os.getenv("REDDIT_FETCH_LIMIT", "100"))
REDDIT_POST_LIMIT = int(os.getenv("REDDIT_POST_LIMIT", "100"))
CHAKRIE_API_TOKEN = os.getenv("CHAKRIE_API_TOKEN", "")
CHAKRIE_TIMELINE_API_URL = os.getenv(
    "CHAKRIE_TIMELINE_API_URL",
    "https://www.chakrie.com/api/v1/mobile/timeline/posts/create",
)
ROOT = Path(__file__).resolve().parent
MEDIA_ROOT = ROOT / "timeline_media" / "reddit_jobs"
TRACKING_PATH = ROOT / "cache" / "chakrie_timeline_posts.json"
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
    subreddit: str = "jobs"

    def timeline_body(self, max_length: int = 1800) -> str:
        attribution = (
            f"\n\nPosted by {self.author} on r/{self.subreddit}\nSource: {self.url}"
        )
        header = self.title
        remaining = max(max_length - len(header) - len(attribution) - 2, 0)
        excerpt = self.text[:remaining].rstrip()
        if len(self.text) > remaining:
            excerpt = excerpt.rstrip(" .") + "…"
        return f"{header}\n\n{excerpt}{attribution}" if excerpt else f"{header}{attribution}"


class Reddit:
    def __init__(
        self,
        timeout: int = 30,
        reddit_url: str = REDDIT_URL,
        fetch_limit: int = REDDIT_FETCH_LIMIT,
    ):
        parsed_url = urlparse(reddit_url)
        host = (parsed_url.hostname or "").casefold()
        match = re.match(r"^/r/([A-Za-z0-9_]+)(?:/|$)", parsed_url.path)
        if host not in {"reddit.com", "www.reddit.com"} or not match:
            raise ValueError(
                "Reddit URL must look like https://www.reddit.com/r/jobhunting/"
            )
        subreddit = match.group(1)
        fetch_limit = min(max(fetch_limit, 1), 100)
        self.timeout = timeout
        self.subreddit = subreddit
        self.rss_url = (
            f"https://www.reddit.com/r/{subreddit}/new/.rss?limit={fetch_limit}"
        )
        self.cache_path = ROOT / "cache" / f"reddit_{subreddit.casefold()}.rss"
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
                result.append(
                    RedditPost(
                        post_id, title, author, text, url, image_url, self.subreddit
                    )
                )
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
            response = self.session.get(self.rss_url, timeout=self.timeout)
            if response.status_code != 429:
                response.raise_for_status()
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_bytes(response.content)
                return response.content

        if self.cache_path.exists() and self.cache_path.stat().st_size > 0:
            LOG.warning(
                "Reddit still returned HTTP 429; using the last cached RSS feed."
            )
            return self.cache_path.read_bytes()
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
        if destination.suffix.casefold() == ".gif":
            # Chakrie currently returns HTTP 500 for GIF timeline uploads.
            # Preserve the visual by uploading the first frame as PNG.
            converted = destination.with_suffix(".png")
            if not converted.exists() or converted.stat().st_size == 0:
                with Image.open(destination) as source:
                    source.seek(0)
                    source.convert("RGBA").save(converted, format="PNG")
            return converted
        return destination


class Tracker:
    """Small local duplicate guard; publishing never requires a database."""

    def __init__(self, path: Path = TRACKING_PATH):
        self.path = path
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            self.records = value if isinstance(value, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            self.records = {}

    def posted_ids(self) -> set[str]:
        return {
            post_id
            for post_id, record in self.records.items()
            if isinstance(record, dict) and record.get("status") == "posted"
        }

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
        self.records[post.post_id] = {
            "reddit_url": post.url,
            "chakrie_post_id": remote_id,
            "body_hash": body_hash,
            "media_path": media_path,
            "status": status,
            "response": response,
            "error": error,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


class Chakrie:
    def __init__(self):
        if not CHAKRIE_API_TOKEN:
            raise RuntimeError("CHAKRIE_API_TOKEN is missing from .env")
        self.headers = {
            "Authorization": f"Bearer {CHAKRIE_API_TOKEN}",
            "Accept": "application/json",
            "User-Agent": "LocalTimelinePublisher/1.0",
        }

    def post(self, body: str, media_path: Path | None) -> dict:
        data = {"body": body, "visibility": "public"}
        if media_path is None:
            # This endpoint reads timeline fields through its multipart parser.
            # Passing fields as (None, value) creates multipart parts without
            # inventing a dummy media attachment.
            response = requests.post(
                CHAKRIE_TIMELINE_API_URL,
                headers=self.headers,
                files={
                    "body": (None, body),
                    "visibility": (None, "public"),
                },
                timeout=60,
            )
        else:
            mime = mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
            with media_path.open("rb") as media:
                response = requests.post(
                    CHAKRIE_TIMELINE_API_URL,
                    headers=self.headers,
                    data=data,
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
        ((response.get("data") or {}).get("post") or {}).get("id")
        if isinstance(response.get("data"), dict)
        and isinstance((response.get("data") or {}).get("post"), dict)
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
        description="Publish attributed subreddit RSS entries to Chakrie timeline."
    )
    parser.add_argument("--post-id", help="Publish/preview one Reddit ID, e.g. t3_abc.")
    parser.add_argument(
        "--reddit-url",
        default=REDDIT_URL,
        help=f"Subreddit URL (default: {REDDIT_URL}).",
    )
    parser.add_argument(
        "--fetch-limit",
        type=int,
        default=REDDIT_FETCH_LIMIT,
        help="RSS entries to fetch; Reddit allows at most 100.",
    )
    parser.add_argument(
        "--subreddit",
        help="Short compatibility option, e.g. --subreddit RemoteJobs.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=REDDIT_POST_LIMIT,
        help=f"Number to preview/post (default: {REDDIT_POST_LIMIT}).",
    )
    parser.add_argument("--send", action="store_true", help="Actually create timeline posts.")
    parser.add_argument(
        "--allow-text-only",
        action="store_true",
        help="Include RSS entries without an image (needed for large runs).",
    )
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

    try:
        reddit_url = args.reddit_url
        if args.subreddit:
            reddit_url = f"https://www.reddit.com/r/{args.subreddit}/"
        reddit = Reddit(
            int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")),
            reddit_url=reddit_url,
            fetch_limit=args.fetch_limit,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    tracker = Tracker()
    failures = 0
    try:
        posts = reddit.posts()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    if args.post_id:
        posts = [post for post in posts if post.post_id == args.post_id]
    if not args.allow_text_only:
        posts = [post for post in posts if post.image_url]
    if not args.force:
        posts = [post for post in posts if post.post_id not in tracker.posted_ids()]
    posts = posts[: max(args.limit, 0)]
    if not posts:
        raise SystemExit("No matching unposted RSS entry was found.")
    LOG.info("Selected %d Reddit post(s)", len(posts))
    client = Chakrie() if args.send else None
    for post in posts:
        body = post.timeline_body()
        digest = hashlib.sha256(body.encode()).hexdigest()
        try:
            media_path = reddit.download(post)
        except Exception as exc:
            if args.allow_text_only:
                media_path = None
                LOG.warning(
                    "Image unavailable for %s; posting text only: %s",
                    post.post_id,
                    exc,
                )
            else:
                failures += 1
                error = f"Media download failed: {exc}"
                tracker.record(post, digest, None, "failed", None, error=error)
                LOG.error("Skipped %s: %s", post.post_id, error)
                continue
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
            tracker.record(
                post, digest, relative, "posted", result, remote_id=post_id
            )
            LOG.info("Posted %s (Chakrie ID: %s)", post.post_id, post_id)
        except Exception as exc:
            failures += 1
            tracker.record(post, digest, relative, "failed", None, error=str(exc))
            LOG.error("Failed %s: %s", post.post_id, exc)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
