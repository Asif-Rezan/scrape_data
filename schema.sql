CREATE DATABASE IF NOT EXISTS product_data
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE product_data;

CREATE TABLE IF NOT EXISTS phones (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    source_url VARCHAR(768) NOT NULL,
    slug VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    brand VARCHAR(120) NULL,
    model VARCHAR(255) NULL,
    device_type VARCHAR(80) NULL,
    status_text VARCHAR(255) NULL,
    price_text VARCHAR(255) NULL,
    price_bdt DECIMAL(12, 2) NULL,
    image_path VARCHAR(1000) NULL,
    announced VARCHAR(255) NULL,
    released VARCHAR(255) NULL,
    operating_system TEXT NULL,
    chipset TEXT NULL,
    ram TEXT NULL,
    storage TEXT NULL,
    display TEXT NULL,
    main_camera TEXT NULL,
    selfie_camera TEXT NULL,
    battery TEXT NULL,
    colors TEXT NULL,
    source_last_modified DATETIME NULL,
    scraped_at DATETIME NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_phones_source_url (source_url(500)),
    KEY idx_phones_brand (brand),
    KEY idx_phones_name (name),
    KEY idx_phones_price (price_bdt),
    KEY idx_phones_device_type (device_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS phone_specs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    phone_id BIGINT UNSIGNED NOT NULL,
    section_name VARCHAR(120) NOT NULL,
    spec_name VARCHAR(190) NOT NULL,
    spec_value LONGTEXT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_phone_spec (phone_id, section_name, spec_name),
    KEY idx_specs_name (spec_name),
    CONSTRAINT fk_phone_specs_phone
      FOREIGN KEY (phone_id) REFERENCES phones(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS phone_images (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    phone_id BIGINT UNSIGNED NOT NULL,
    image_path VARCHAR(1000) NOT NULL,
    sort_order SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (id),
    UNIQUE KEY uq_phone_image (phone_id, image_path(500)),
    CONSTRAINT fk_phone_images_phone
      FOREIGN KEY (phone_id) REFERENCES phones(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS scrape_failures (
    source_url VARCHAR(768) NOT NULL,
    error_message TEXT NOT NULL,
    attempts INT UNSIGNED NOT NULL DEFAULT 1,
    last_attempt_at DATETIME NOT NULL,
    PRIMARY KEY (source_url(500))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
