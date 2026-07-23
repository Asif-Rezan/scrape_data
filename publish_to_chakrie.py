#!/usr/bin/env python3
"""Publish jobs_data records to the authenticated Chakrie employer API."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
from datetime import date, datetime
from typing import Any

import mysql.connector
import requests
from dotenv import load_dotenv


load_dotenv()

# Change these values in .env when the endpoint or token changes.
CHAKRIE_API_TOKEN = os.getenv("CHAKRIE_API_TOKEN", "")
CHAKRIE_API_URL = os.getenv(
    "CHAKRIE_API_URL",
    "https://www.chakrie.com/api/v1/mobile/employer/jobs",
)

# Chakrie values observed from its employer API. These mappings are kept at
# the top of the script so they are easy to adjust if the API changes.
JOB_TYPE_MAP = {
    "full time/permanent": "1",
    "full time": "1",
    "part time": "2",
    "contractual": "3",
    "contract": "3",
    "internship": "4",
}
WORK_MODE_MAP = {
    "from home": "1",
    "remote": "1",
    "hybrid": "2",
    "on-site": "3",
    "onsite": "3",
    "at office": "3",
}

LOG = logging.getLogger("chakrie-publisher")


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def paragraphs(value: Any) -> str:
    text = clean(value)
    if not text:
        return ""
    pieces = [clean(piece) for piece in re.split(r"(?:\r?\n)+|(?=✔)", str(value))]
    return "".join(f"<p>{html.escape(piece)}</p>" for piece in pieces if piece)


def numeric_vacancy(value: Any) -> int:
    match = re.search(r"\d+", clean(value))
    return max(int(match.group()), 1) if match else 1


def experience_level(value: Any) -> str:
    numbers = [int(x) for x in re.findall(r"\d+", clean(value))]
    years = max(numbers) if numbers else 0
    if years <= 1:
        return "0"  # Entry Level
    if years <= 4:
        return "1"  # Mid Level
    return "2"  # Senior Level


def job_type(value: Any) -> str:
    normalized = clean(value).casefold()
    for label, api_value in JOB_TYPE_MAP.items():
        if label in normalized:
            return api_value
    return "1"


def work_mode(value: Any) -> str:
    normalized = clean(value).casefold()
    for label, api_value in WORK_MODE_MAP.items():
        if label in normalized:
            return api_value
    return "3"


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
        self.ensure_tracking_table()

    def close(self):
        self.connection.close()

    def ensure_tracking_table(self):
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS chakrie_posts (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    job_id BIGINT UNSIGNED NOT NULL,
                    chakrie_job_id BIGINT UNSIGNED NULL,
                    payload_hash CHAR(64) NOT NULL,
                    status VARCHAR(40) NOT NULL,
                    response_json JSON NULL,
                    error_message TEXT NULL,
                    posted_at DATETIME NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    UNIQUE KEY uq_chakrie_posts_job (job_id),
                    KEY idx_chakrie_remote_id (chakrie_job_id),
                    CONSTRAINT fk_chakrie_posts_job
                      FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                  COLLATE=utf8mb4_unicode_ci
                """
            )
            self.connection.commit()
        finally:
            cursor.close()

    def jobs(self, source_job_id: int | None, limit: int | None) -> list[dict]:
        sql = """
            SELECT j.*,
                   cp.status AS chakrie_status,
                   cp.chakrie_job_id,
                   cp.payload_hash AS previous_payload_hash
            FROM jobs j
            LEFT JOIN chakrie_posts cp ON cp.job_id=j.id
            WHERE j.application_deadline >= CURDATE()
        """
        values: list[Any] = []
        if source_job_id is not None:
            sql += " AND j.source_job_id=%s"
            values.append(source_job_id)
        sql += " ORDER BY j.published_at DESC, j.id DESC"
        if limit is not None:
            sql += " LIMIT %s"
            values.append(max(limit, 0))
        cursor = self.connection.cursor(dictionary=True)
        try:
            cursor.execute(sql, values)
            jobs = cursor.fetchall()
            for job in jobs:
                job["sections"] = self.sections(job["id"])
            return jobs
        finally:
            cursor.close()

    def sections(self, job_id: int) -> dict[str, str]:
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                "SELECT section_name,section_text FROM job_sections WHERE job_id=%s",
                (job_id,),
            )
            return {clean(name).casefold(): clean(text) for name, text in cursor}
        finally:
            cursor.close()

    def record(
        self,
        job_id: int,
        payload_hash: str,
        status: str,
        response: dict | list | None,
        remote_id: int | None = None,
        error: str | None = None,
    ):
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO chakrie_posts
                    (job_id,chakrie_job_id,payload_hash,status,response_json,
                     error_message,posted_at)
                VALUES(%s,%s,%s,%s,%s,%s,
                       IF(%s='posted',UTC_TIMESTAMP(),NULL))
                ON DUPLICATE KEY UPDATE
                    chakrie_job_id=COALESCE(VALUES(chakrie_job_id),chakrie_job_id),
                    payload_hash=VALUES(payload_hash),
                    status=VALUES(status),
                    response_json=VALUES(response_json),
                    error_message=VALUES(error_message),
                    posted_at=IF(VALUES(status)='posted',UTC_TIMESTAMP(),posted_at)
                """,
                (
                    job_id,
                    remote_id,
                    payload_hash,
                    status,
                    json.dumps(response, ensure_ascii=False) if response is not None else None,
                    error,
                    status,
                ),
            )
            self.connection.commit()
        finally:
            cursor.close()


def section(job: dict, *names: str) -> str:
    sections = job.get("sections", {})
    for name in names:
        if clean(name).casefold() in sections:
            return sections[clean(name).casefold()]
    return ""


def payload(job: dict) -> dict:
    responsibilities = section(job, "Responsibilities & Context")
    education = section(job, "Education") or job.get("education_summary")
    requirements = section(job, "Additional Requirements", "Experience")
    procedure = section(job, "Application Procedure")
    skills = section(job, "Skills")
    description_text = responsibilities or job.get("full_text") or job["title"]
    company_info = section(job, "Company Information")
    deadline = job.get("application_deadline")
    if isinstance(deadline, (datetime, date)):
        deadline = deadline.strftime("%Y-%m-%d")
    tags = ",".join(
        dict.fromkeys(
            item.strip()
            for item in re.split(r"[,|•]+", skills or clean(job.get("functional_area")))
            if item.strip()
        )
    )[:500]
    return {
        "title": clean(job["title"]),
        "description": paragraphs(description_text),
        "location": clean(job.get("locations")) or "Bangladesh",
        "job_type": job_type(job.get("job_type")),
        "work_mode": work_mode(job.get("workplace")),
        "experience_level": experience_level(job.get("experience_summary")),
        "company_name": clean(job.get("company_name")) or "Confidential",
        "company_address": company_info,
        "category_choice": clean(job.get("functional_area")) or "Other",
        "url": clean(job.get("source_url")),
        "last_date": deadline,
        "vacancy_count": numeric_vacancy(job.get("vacancy")),
        "salary_min": float(job["salary_from"]) if job.get("salary_from") is not None else None,
        "salary_max": float(job["salary_to"]) if job.get("salary_to") is not None else None,
        "salary_currency": "BDT",
        "salary_period": "monthly",
        "responsibilities": paragraphs(responsibilities),
        "education_requirements": paragraphs(education),
        "experience_requirements": paragraphs(requirements),
        "application_procedure": "chakrie",
        "tags": tags,
    }


class Chakrie:
    def __init__(self):
        if not CHAKRIE_API_TOKEN:
            raise RuntimeError("CHAKRIE_API_TOKEN is missing from .env")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {CHAKRIE_API_TOKEN}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "LocalJobsPublisher/1.0",
            }
        )

    def post(self, body: dict) -> dict:
        response = self.session.post(CHAKRIE_API_URL, json=body, timeout=45)
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


def response_id(result: dict) -> int | None:
    candidates = [
        result.get("id"),
        (result.get("data") or {}).get("id") if isinstance(result.get("data"), dict) else None,
        (result.get("job") or {}).get("id") if isinstance(result.get("job"), dict) else None,
    ]
    for value in candidates:
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return None


def arguments():
    parser = argparse.ArgumentParser(description="Publish jobs_data jobs to Chakrie.")
    parser.add_argument("--job-id", type=int, help="One BDJobsLive source job ID.")
    parser.add_argument("--limit", type=int, help="Limit selected active jobs.")
    parser.add_argument("--send", action="store_true", help="Actually call the POST API.")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Required with --send when no --job-id is supplied.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Post again even when the job was already posted.",
    )
    return parser.parse_args()


def main() -> int:
    args = arguments()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    if args.send and args.job_id is None and not args.all:
        raise SystemExit("Refusing bulk posting: add --all or select one --job-id.")

    database = Database()
    failures = 0
    try:
        jobs = database.jobs(args.job_id, args.limit)
        if not jobs:
            raise SystemExit("No matching active job was found.")
        LOG.info("Selected %d job(s)", len(jobs))
        client = Chakrie() if args.send else None
        for job in jobs:
            body = payload(job)
            encoded = json.dumps(body, ensure_ascii=False, sort_keys=True).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            if job.get("chakrie_status") == "posted" and not args.force:
                LOG.info("Skipped already posted: %s", job["title"])
                continue
            if not args.send:
                print(json.dumps(body, ensure_ascii=False, indent=2))
                if len(jobs) > 1:
                    print("---")
                continue
            try:
                result = client.post(body)
                remote_id = response_id(result)
                database.record(job["id"], digest, "posted", result, remote_id)
                LOG.info("Posted: %s (Chakrie ID: %s)", job["title"], remote_id)
            except Exception as exc:
                failures += 1
                database.record(job["id"], digest, "failed", None, error=str(exc))
                LOG.error("Failed: %s — %s", job["title"], exc)
    finally:
        database.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

