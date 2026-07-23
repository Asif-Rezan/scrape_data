# MobileDokan to MySQL scraper

This project reads MobileDokan's public product sitemaps, visits each product
page politely, and upserts smartphones and feature phones into the local XAMPP
MySQL database `product_data`. Product gallery images are downloaded into the
local `images` directory; MySQL stores relative file paths, not remote URLs.

## Setup (PowerShell)

Make sure Apache and MySQL are running in the XAMPP Control Panel, then run:

```powershell
cd E:\scrape_data
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

The supplied `.env.example` already contains the default XAMPP settings:
`root`, blank password, port `3306`, and database `product_data`.

## Test one phone

```powershell
python .\scrape_mobiledokan.py --url "https://www.mobiledokan.co/product/motorola-razr-2026/" -v
```

Check the result in phpMyAdmin:

```sql
SELECT id, brand, name, price_bdt, scraped_at FROM phones;
```

## Scrape all phones

```powershell
python .\scrape_mobiledokan.py
```

The full run can take hours because requests are intentionally delayed. It is
safe to stop with `Ctrl+C` and rerun: products are updated rather than
duplicated. Pages that fail are recorded in `scrape_failures`.

Useful options:

```powershell
# Test the first 10 product pages from the sitemaps
python .\scrape_mobiledokan.py --limit 10

# Include tablets and wearables as well as phones
python .\scrape_mobiledokan.py --all-products

# Use a longer polite delay
python .\scrape_mobiledokan.py --delay 2.5
```

The `phones.image_path` column contains the primary image path. The
`phone_images` table contains every downloaded gallery image in display order.
The `phones` table contains frequently queried fields. The `phone_specs` table
preserves every specification found on each page without forcing all possible
specifications into hundreds of database columns.

## BDJobsLive scraper

BDJobsLive jobs use separate `jobs`, `job_sections`, and
`job_scrape_failures` tables in the `jobs_data` database. Company
logos are downloaded to `job_images/company_logos`, and only local relative
paths are stored.

Test with two current jobs:

```powershell
python .\scrape_bdjobslive.py --limit 2 -v
```

Import all currently active jobs:

```powershell
python .\scrape_bdjobslive.py
```

The importer is safe to rerun. Existing jobs are updated using BDJobsLive's
public job ID, and complete detail-page sections are stored in `job_sections`.

## Publish jobs to Chakrie

The bearer token and endpoint are kept in `.env`. Preview one mapped job
without changing Chakrie:

```powershell
python .\publish_to_chakrie.py --job-id 12191
```

Publish that one job:

```powershell
python .\publish_to_chakrie.py --job-id 12191 --send
```

Bulk publishing requires an explicit additional confirmation flag:

```powershell
python .\publish_to_chakrie.py --all --send
```

Successful and failed API calls are tracked in `jobs_data.chakrie_posts`, so
successful jobs are skipped on later runs unless `--force` is supplied.

## Publish r/jobs posts to the Chakrie timeline

The timeline publisher reads Reddit's permitted `r/jobs` RSS feed, selects
entries with images, downloads the image, and includes author/source
attribution in the timeline body.

Preview the newest eligible entry without posting:

```powershell
python .\publish_reddit_timeline.py
```

Post one displayed Reddit ID:

```powershell
python .\publish_reddit_timeline.py --post-id t3_example --send
```

Post up to five currently eligible entries:

```powershell
python .\publish_reddit_timeline.py --all --limit 5 --send
```

Posted Reddit IDs and API responses are tracked in
`jobs_data.chakrie_timeline_posts` to prevent duplicates.
