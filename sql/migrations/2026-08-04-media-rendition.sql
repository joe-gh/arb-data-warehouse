-- WordPress-computed product image renditions for the dev CDN pilot.
--
-- canonical_key is always an existing pim.media_object.s3_key. It is not a
-- foreign key because media_object intentionally maps many source_url rows to
-- the same non-unique s3_key. rendition_file is a sibling basename only; the
-- generator refuses any destination equal to canonical_key.
BEGIN;

CREATE TABLE IF NOT EXISTS pim.media_rendition (
    canonical_key  text        NOT NULL,
    rendition_file text        NOT NULL,
    width           integer     NOT NULL,
    height          integer     NOT NULL,
    size_name       text        NOT NULL DEFAULT '',
    content_type    text        NOT NULL DEFAULT '',
    crop            boolean     NOT NULL DEFAULT false,
    crop_x          text        NOT NULL DEFAULT 'center',
    crop_y          text        NOT NULL DEFAULT 'center',
    generated       boolean     NOT NULL DEFAULT false,
    bytes           bigint      NOT NULL DEFAULT 0,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (canonical_key, rendition_file),
    CHECK (canonical_key ~ '^products/[^/]+/[^/]+$'),
    CHECK (rendition_file <> ''
           AND strpos(rendition_file, '/') = 0
           AND strpos(rendition_file, chr(92)) = 0
           AND strpos(rendition_file, '..') = 0),
    CHECK (width > 0 AND height > 0),
    CHECK (bytes >= 0),
    CHECK (crop_x IN ('left', 'center', 'right')),
    CHECK (crop_y IN ('top', 'center', 'bottom'))
);

CREATE INDEX IF NOT EXISTS media_rendition_pending
    ON pim.media_rendition (canonical_key, rendition_file)
    WHERE generated = false;

GRANT SELECT ON pim.media_rendition TO woo_reader, insights_reader, logo_admin;
GRANT SELECT, INSERT, UPDATE, DELETE ON pim.media_rendition TO pim_writer;

COMMIT;
