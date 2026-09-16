-- Preserve real Spotlight dictionaries as JSON objects, not disguised strings.
-- SQLite cannot widen a CHECK in place. attribute has no inbound foreign keys
-- or custom indexes/triggers; rebuild just this table in the migration transaction.
CREATE TABLE attribute_v2 (
    occ_id     TEXT NOT NULL,
    rel_path   TEXT NOT NULL,
    key        TEXT NOT NULL,
    value_json TEXT NOT NULL,
    value_type TEXT NOT NULL CHECK (value_type IN (
                   'string', 'number', 'bool', 'date', 'list', 'dict', 'null')),
    source     TEXT NOT NULL CHECK (source IN ('mdls', 'importer', 'xattr', 'stat')),
    PRIMARY KEY (occ_id, rel_path, key, source),
    FOREIGN KEY (occ_id, rel_path) REFERENCES entry (occ_id, rel_path)
);
INSERT INTO attribute_v2 (occ_id, rel_path, key, value_json, value_type, source)
    SELECT occ_id, rel_path, key, value_json, value_type, source FROM attribute;
DROP TABLE attribute;
ALTER TABLE attribute_v2 RENAME TO attribute;
