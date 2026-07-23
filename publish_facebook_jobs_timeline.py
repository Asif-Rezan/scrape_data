#!/usr/bin/env python3
"""Publish job-related Facebook Page posts to the Chakrie timeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from PIL import Image

from publish_reddit_timeline import Chakrie, remote_id


load_dotenv()

ROOT = Path(__file__).resolve().parent
MEDIA_ROOT = ROOT / "timeline_media" / "facebook_jobs"
TRACKING_PATH = ROOT / "cache" / "chakrie_facebook_posts.json"

# Configure these in .env. A Page access token with the permissions granted to
# your Meta app is required; the script never scrapes Facebook's HTML.
FACEBOOK_PAGE_ID = os.getenv("FACEBOOK_PAGE_ID", "")
FACEBOOK_PAGE_ACCESS_TOKEN = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "")
FACEBOOK_GRAPH_VERSION = os.getenv("FACEBOOK_GRAPH_VERSION", "v23.0")
FACEBOOK_FETCH_LIMIT = int(os.getenv("FACEBOOK_FETCH_LIMIT", "100"))
FACEBOOK_POST_LIMIT = int(os.getenv("FACEBOOK_POST_LIMIT", "100"))

JOB_KEYWORDS = (
    "apply now",
    "career opportunity",
    "hiring",
    "job opening",
    "job vacancy",
    "position available",
    "recruitment",
    "vacancy",
    "we are looking",
    "চাকরি",
    "নিয়োগ",
    "নিয়োগ",
    "আবেদন",
)

LOG = logging.getLogger("facebook-timeline")


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


@dataclass
class FacebookPost:
    post_id: str
    message: str
    created_time: str
    permalink_url: str
    image_url: str | None
    page_name: str

    def is_job_post(self) -> bool:
        value = self.message.casefold()
        return any(keyword.casefold() in value for keyword in JOB_KEYWORDS)

    def timeline_body(self, max_length: int = 1800) -> str:
        attribution = (
            f"\n\nPosted by {self.page_name} on Facebook"
            f"\nSource: {self.permalink_url}"
        )
        remaining = max(max_length - len(attribution), 0)
        message = self.message[:remaining].rstrip()
        if len(self.message) > remaining:
            message = message.rstrip(" .") + "…"
        return f"{message}{attribution}"


class Facebook:
    def __init__(
        self,
        page_id: str,
        access_token: str,
        graph_version: str,
        fetch_limit: int,
        timeout: int = 30,
    ):
        if not clean(page_id):
            raise ValueError("FACEBOOK_PAGE_ID is missing from .env")
        if not clean(access_token):
            raise ValueError("FACEBOOK_PAGE_ACCESS_TOKEN is missing from .env")
        if not re.fullmatch(r"v\d+\.\d+", graph_version):
            raise ValueError("FACEBOOK_GRAPH_VERSION must look like v23.0")
        self.page_id = clean(page_id)
        self.access_token = clean(access_token)
        self.graph_version = graph_version
        self.fetch_limit = max(fetch_limit, 0)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "ChakrieFacebookPageImporter/1.0",
            }
        )

    def posts(self) -> list[FacebookPost]:
        page_name = self._page_name()
        fields = ",".join(
            (
                "id",
                "message",
                "created_time",
                "permalink_url",
                "full_picture",
            )
        )
        url = (
            f"https://graph.facebook.com/{self.graph_version}/"
            f"{self.page_id}/posts"
        )
        params = {
            "fields": fields,
            "limit": min(max(self.fetch_limit, 1), 100),
            "access_token": self.access_token,
        }
        result: list[FacebookPost] = []
        while url and len(result) < self.fetch_limit:
            response = self.session.get(
                url,
                params=params,
                timeout=self.timeout,
            )
            payload = self._payload(response)
            for item in payload.get("data", []):
                post_id = clean(item.get("id"))
                message = clean(item.get("message"))
                permalink = clean(item.get("permalink_url"))
                if post_id and message and permalink:
                    result.append(
                        FacebookPost(
                            post_id=post_id,
                            message=message,
                            created_time=clean(item.get("created_time")),
                            permalink_url=permalink,
                            image_url=clean(item.get("full_picture")) or None,
                            page_name=page_name,
                        )
                    )
                if len(result) >= self.fetch_limit:
                    break
            next_url = (payload.get("paging") or {}).get("next")
            url = clean(next_url)
            params = None
        return result

    def _page_name(self) -> str:
        url = (
            f"https://graph.facebook.com/{self.graph_version}/"
            f"{self.page_id}"
        )
        response = self.session.get(
            url,
            params={"fields": "name", "access_token": self.access_token},
            timeout=self.timeout,
        )
        payload = self._payload(response)
        return clean(payload.get("name")) or f"Facebook Page {self.page_id}"

    @staticmethod
    def _payload(response: requests.Response) -> dict:
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw_response": response.text[:2000]}
        if not response.ok:
            error = payload.get("error") if isinstance(payload, dict) else payload
            raise requests.HTTPError(
                f"Facebook HTTP {response.status_code}: "
                f"{json.dumps(error, ensure_ascii=False)}",
                response=response,
            )
        return payload

    def download(self, post: FacebookPost) -> Path | None:
        if not post.image_url:
            return None
        host = (urlparse(post.image_url).hostname or "").casefold()
        if not (
            host == "facebook.com"
            or host.endswith(".facebook.com")
            or host == "fbcdn.net"
            or host.endswith(".fbcdn.net")
        ):
            raise ValueError(f"Unexpected Facebook media host: {host}")
        response = self.session.get(post.image_url, timeout=self.timeout)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
        if content_type and not content_type.startswith("image/"):
            raise ValueError(f"Facebook media is not an image: {content_type}")
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
            converted = destination.with_suffix(".png")
            if not converted.exists() or converted.stat().st_size == 0:
                with Image.open(destination) as source:
                    source.seek(0)
                    source.convert("RGBA").save(converted, format="PNG")
            return converted
        return destination


class Tracker:
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
        post: FacebookPost,
        status: str,
        response: dict | None,
        media_path: str | None,
        remote_post_id: int | None = None,
        error: str | None = None,
    ):
        self.records[post.post_id] = {
            "facebook_url": post.permalink_url,
            "chakrie_post_id": remote_post_id,
            "status": status,
            "response": response,
            "media_path": media_path,
            "error": error,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def arguments():
    parser = argparse.ArgumentParser(
        description="Publish job-related Facebook Page posts to Chakrie."
    )
    parser.add_argument("--post-id", help="Select one Facebook post ID.")
    parser.add_argument("--page-id", default=FACEBOOK_PAGE_ID)
    parser.add_argument("--fetch-limit", type=int, default=FACEBOOK_FETCH_LIMIT)
    parser.add_argument("--limit", type=int, default=FACEBOOK_POST_LIMIT)
    parser.add_argument(
        "--all-posts",
        action="store_true",
        help="Include posts that do not contain a recognized job keyword.",
    )
    parser.add_argument("--send", action="store_true")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Required with --send unless --post-id is provided.",
    )
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = arguments()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    if args.send and not args.post_id and not args.all:
        raise SystemExit("Refusing bulk publishing: add --all or select --post-id.")

    try:
        facebook = Facebook(
            page_id=args.page_id,
            access_token=FACEBOOK_PAGE_ACCESS_TOKEN,
            graph_version=FACEBOOK_GRAPH_VERSION,
            fetch_limit=args.fetch_limit,
            timeout=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from None

    tracker = Tracker()
    posts = facebook.posts()
    if args.post_id:
        posts = [post for post in posts if post.post_id == args.post_id]
    if not args.all_posts:
        posts = [post for post in posts if post.is_job_post()]
    if not args.force:
        posted = tracker.posted_ids()
        posts = [post for post in posts if post.post_id not in posted]
    posts = posts[: max(args.limit, 0)]
    if not posts:
        raise SystemExit("No matching unposted Facebook Page post was found.")

    LOG.info("Selected %d Facebook post(s)", len(posts))
    chakrie = Chakrie() if args.send else None
    failures = 0
    for post in posts:
        media_path = None
        try:
            media_path = facebook.download(post)
        except Exception as exc:
            LOG.warning(
                "Image unavailable for %s; posting text only: %s",
                post.post_id,
                exc,
            )
        relative = (
            media_path.relative_to(ROOT).as_posix() if media_path else None
        )
        body = post.timeline_body()
        if not args.send:
            print(
                json.dumps(
                    {
                        "facebook_post_id": post.post_id,
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
            result = chakrie.post(body, media_path)
            chakrie_id = remote_id(result)
            tracker.record(
                post,
                "posted",
                result,
                relative,
                remote_post_id=chakrie_id,
            )
            LOG.info(
                "Posted %s (Chakrie ID: %s)",
                post.post_id,
                chakrie_id,
            )
        except Exception as exc:
            failures += 1
            tracker.record(
                post,
                "failed",
                None,
                relative,
                error=str(exc),
            )
            LOG.error("Failed %s: %s", post.post_id, exc)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
