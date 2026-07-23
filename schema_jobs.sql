CREATE DATABASE IF NOT EXISTS jobs_data
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE jobs_data;

CREATE TABLE IF NOT EXISTS jobs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    source_job_id BIGINT UNSIGNED NOT NULL,
    source_url VARCHAR(768) NOT NULL,
    slug VARCHAR(500) NOT NULL,
    title VARCHAR(500) NOT NULL,
    company_id BIGINT UNSIGNED NULL,
    company_name VARCHAR(500) NULL,
    company_slug VARCHAR(500) NULL,
    company_logo_path VARCHAR(1000) NULL,
    functional_area VARCHAR(255) NULL,
    industry VARCHAR(255) NULL,
    job_type VARCHAR(255) NULL,
    job_shift VARCHAR(255) NULL,
    workplace VARCHAR(255) NULL,
    locations TEXT NULL,
    vacancy VARCHAR(100) NULL,
    age_requirement VARCHAR(255) NULL,
    gender_preference VARCHAR(100) NULL,
    experience_summary VARCHAR(255) NULL,
    salary_from DECIMAL(14,2) NULL,
    salary_to DECIMAL(14,2) NULL,
    salary_text VARCHAR(500) NULL,
    salary_negotiable BOOLEAN NOT NULL DEFAULT FALSE,
    is_featured BOOLEAN NOT NULL DEFAULT FALSE,
    education_summary TEXT NULL,
    published_at DATETIME NULL,
    application_deadline DATETIME NULL,
    full_text LONGTEXT NULL,
    raw_listing_json JSON NULL,
    scraped_at DATETIME NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_jobs_source_id (source_job_id),
    UNIQUE KEY uq_jobs_source_url (source_url(500)),
    KEY idx_jobs_company (company_name),
    KEY idx_jobs_functional_area (functional_area),
    KEY idx_jobs_deadline (application_deadline),
    KEY idx_jobs_published (published_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS job_sections (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    job_id BIGINT UNSIGNED NOT NULL,
    section_name VARCHAR(255) NOT NULL,
    section_text LONGTEXT NULL,
    sort_order SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY uq_job_section (job_id, section_name),
    CONSTRAINT fk_job_sections_job
      FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS job_scrape_failures (
    source_url VARCHAR(768) NOT NULL,
    error_message TEXT NOT NULL,
    attempts INT UNSIGNED NOT NULL DEFAULT 1,
    last_attempt_at DATETIME NOT NULL,
    PRIMARY KEY (source_url(500))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
