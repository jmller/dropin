-- Initial schema. Applied inside one transaction.
--
-- Identity is globally namespaced: <32-hex store>.<26-char Crockford ULID>, so
-- two stores can be merged during recovery without id collisions, and attempt
-- sequence uniqueness is scoped to the origin store rather than global.

CREATE TABLE store_meta (
    store_id           TEXT PRIMARY KEY,
    export_seq         INTEGER NOT NULL DEFAULT 0,
    published_frontier INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL
);

CREATE TABLE lineage (
    store_id           TEXT PRIMARY KEY,
    adopted_at         TEXT NOT NULL,
    merged_through_seq INTEGER NOT NULL
);

CREATE TABLE occurrence (
    occ_id               TEXT PRIMARY KEY,
    origin_store_id      TEXT NOT NULL,
    item_name            TEXT NOT NULL,
    archive_path         TEXT NOT NULL UNIQUE,
    kind                 TEXT NOT NULL CHECK (kind IN ('file', 'dir', 'bundle')),
    spool_path           TEXT NOT NULL,
    root_sha256          TEXT NOT NULL,
    size_bytes           INTEGER NOT NULL,
    entry_count          INTEGER NOT NULL,
    state                TEXT NOT NULL CHECK (state IN (
                             'recorded', 'transferred', 'verified',
                             'recoverable', 'evicting', 'evicted', 'abandoned')),
    confirmed_attempt_id TEXT,
    recorded_at          TEXT,
    transferred_at       TEXT,
    verified_at          TEXT,
    recoverable_at       TEXT,
    evicting_at          TEXT,
    evicted_at           TEXT,
    abandoned_at         TEXT,
    last_error           TEXT,
    dedup_of             TEXT
);

CREATE INDEX occurrence_state ON occurrence (state);
CREATE INDEX occurrence_root_sha256 ON occurrence (root_sha256);
CREATE INDEX occurrence_item_name ON occurrence (item_name);
CREATE INDEX occurrence_recorded_at ON occurrence (recorded_at);

-- The expected manifest. Immutable after `recorded`.
CREATE TABLE entry (
    occ_id         TEXT NOT NULL REFERENCES occurrence (occ_id),
    rel_path       TEXT NOT NULL,
    entry_type     TEXT NOT NULL CHECK (entry_type IN ('file', 'dir', 'symlink')),
    size_bytes     INTEGER,
    sha256         TEXT,
    link_target    TEXT,
    mode           INTEGER NOT NULL,
    archive_path   TEXT NOT NULL UNIQUE,
    searchable     INTEGER NOT NULL DEFAULT 1,
    capture_status TEXT NOT NULL DEFAULT 'ok',
    PRIMARY KEY (occ_id, rel_path)
);

-- Source stability: sampled at `recorded`, compared at every gate.
CREATE TABLE fingerprint (
    occ_id      TEXT NOT NULL,
    rel_path    TEXT NOT NULL,
    entry_type  TEXT NOT NULL,
    size_bytes  INTEGER,
    mtime_ns    INTEGER NOT NULL,
    ctime_ns    INTEGER NOT NULL,
    inode       INTEGER NOT NULL,
    dev         INTEGER NOT NULL,
    link_target TEXT,
    PRIMARY KEY (occ_id, rel_path),
    FOREIGN KEY (occ_id, rel_path) REFERENCES entry (occ_id, rel_path)
);

-- The expected manifest and its fingerprints are evidence about a moment that
-- has passed. Nothing may rewrite them: every later gate compares against these
-- rows, so a mutable manifest would let the archiver agree with itself about a
-- source it never saw. Recovery merges by inserting, never by updating.
CREATE TRIGGER entry_no_update BEFORE UPDATE ON entry BEGIN
    SELECT RAISE(ABORT, 'entry rows are immutable after recorded');
END;

CREATE TRIGGER entry_no_delete BEFORE DELETE ON entry BEGIN
    SELECT RAISE(ABORT, 'entry rows are immutable after recorded');
END;

CREATE TRIGGER fingerprint_no_update BEFORE UPDATE ON fingerprint BEGIN
    SELECT RAISE(ABORT, 'fingerprint rows are immutable after recorded');
END;

CREATE TRIGGER fingerprint_no_delete BEFORE DELETE ON fingerprint BEGIN
    SELECT RAISE(ABORT, 'fingerprint rows are immutable after recorded');
END;

CREATE TABLE attribute (
    occ_id     TEXT NOT NULL,
    rel_path   TEXT NOT NULL,
    key        TEXT NOT NULL,
    value_json TEXT NOT NULL,
    value_type TEXT NOT NULL CHECK (value_type IN (
                   'string', 'number', 'bool', 'date', 'list', 'null')),
    source     TEXT NOT NULL CHECK (source IN ('mdls', 'importer', 'xattr', 'stat')),
    PRIMARY KEY (occ_id, rel_path, key, source),
    FOREIGN KEY (occ_id, rel_path) REFERENCES entry (occ_id, rel_path)
);

CREATE TABLE xattr (
    occ_id    TEXT NOT NULL,
    rel_path  TEXT NOT NULL,
    name      TEXT NOT NULL,
    value_b64 TEXT,
    status    TEXT NOT NULL,
    PRIMARY KEY (occ_id, rel_path, name),
    FOREIGN KEY (occ_id, rel_path) REFERENCES entry (occ_id, rel_path)
);

-- External-content table for the FTS index: it must carry every FTS column.
CREATE TABLE normalized (
    occ_id     TEXT NOT NULL,
    rel_path   TEXT NOT NULL,
    name       TEXT NOT NULL,
    uti        TEXT,
    kind       TEXT,
    created    TEXT,
    modified   TEXT,
    size_bytes INTEGER,
    sha256     TEXT,
    comment    TEXT,
    text       TEXT,
    FOREIGN KEY (occ_id, rel_path) REFERENCES entry (occ_id, rel_path)
);

CREATE UNIQUE INDEX normalized_entry ON normalized (occ_id, rel_path);
CREATE INDEX normalized_uti ON normalized (uti);
CREATE INDEX normalized_created ON normalized (created);
CREATE INDEX normalized_modified ON normalized (modified);
CREATE INDEX normalized_sha256 ON normalized (sha256);
CREATE INDEX normalized_name ON normalized (name COLLATE NOCASE);

CREATE TABLE tag (
    occ_id   TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    tag      TEXT NOT NULL,
    PRIMARY KEY (occ_id, rel_path, tag),
    FOREIGN KEY (occ_id, rel_path) REFERENCES entry (occ_id, rel_path)
);

CREATE VIRTUAL TABLE fulltext USING fts5 (
    name, comment, text,
    content='normalized',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER normalized_ai AFTER INSERT ON normalized BEGIN
    INSERT INTO fulltext (rowid, name, comment, text)
    VALUES (new.rowid, new.name, new.comment, new.text);
END;

CREATE TRIGGER normalized_ad AFTER DELETE ON normalized BEGIN
    INSERT INTO fulltext (fulltext, rowid, name, comment, text)
    VALUES ('delete', old.rowid, old.name, old.comment, old.text);
END;

CREATE TRIGGER normalized_au AFTER UPDATE ON normalized BEGIN
    INSERT INTO fulltext (fulltext, rowid, name, comment, text)
    VALUES ('delete', old.rowid, old.name, old.comment, old.text);
    INSERT INTO fulltext (rowid, name, comment, text)
    VALUES (new.rowid, new.name, new.comment, new.text);
END;

-- One `restic backup` invocation. Committed before the backup runs,
-- so `snapshot_id` is null between start and id capture by design.
CREATE TABLE publication_attempt (
    attempt_id      TEXT PRIMARY KEY,
    origin_store_id TEXT NOT NULL,
    occ_id          TEXT NOT NULL REFERENCES occurrence (occ_id),
    export_seq      INTEGER NOT NULL,
    export_sha256   TEXT,
    export_path     TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    snapshot_id     TEXT,
    outcome         TEXT NOT NULL CHECK (outcome IN ('pending', 'confirmed', 'failed')),
    reason          TEXT,
    finished_at     TEXT
);

CREATE INDEX publication_attempt_occ ON publication_attempt (occ_id, started_at);
CREATE UNIQUE INDEX publication_attempt_seq
    ON publication_attempt (origin_store_id, export_seq);
CREATE UNIQUE INDEX publication_attempt_confirmed
    ON publication_attempt (occ_id) WHERE outcome = 'confirmed';

-- Carried inside every catalog export. Deliberately holds no whole-file digest:
-- that would be self-referential. The digest lives in the immutable snapshot tag
-- and in the live attempt row.
CREATE TABLE export_lineage (
    store_id    TEXT NOT NULL,
    export_seq  INTEGER NOT NULL,
    occ_id      TEXT NOT NULL,
    attempt_id  TEXT NOT NULL UNIQUE,
    exported_at TEXT NOT NULL,
    PRIMARY KEY (store_id, export_seq)
);

CREATE INDEX export_lineage_occ ON export_lineage (occ_id);

-- The repository-observation ledger: every strictly parseable snapshot we have
-- seen, including ones whose export was missing or corrupt.
CREATE TABLE snapshot (
    snapshot_id    TEXT PRIMARY KEY,
    occ_id         TEXT NOT NULL,
    attempt_id     TEXT NOT NULL,
    store_id       TEXT NOT NULL,
    export_seq     INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    catalog_sha256 TEXT NOT NULL,
    tag_set_json   TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN (
                       'pending', 'confirmed', 'orphaned', 'foreign')),
    reason         TEXT,
    seen_at        TEXT NOT NULL
);

CREATE INDEX snapshot_attempt ON snapshot (attempt_id);
CREATE INDEX snapshot_occ ON snapshot (occ_id);
CREATE INDEX snapshot_store_seq ON snapshot (store_id, export_seq);

CREATE TABLE eviction_intent (
    occ_id                           TEXT PRIMARY KEY REFERENCES occurrence (occ_id),
    intent_at                        TEXT NOT NULL,
    fingerprint_json                 TEXT NOT NULL,
    progress_json                    TEXT,
    recovered_without_local_history  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE run (
    run_id          TEXT PRIMARY KEY,
    verb            TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    exit_code       INTEGER,
    writer_lock_pid INTEGER,
    cache_evicted   INTEGER NOT NULL DEFAULT 0,
    -- Peak RSS of child processes (restic) over the run, from getrusage; the
    -- resource-budget evidence `status` reports.
    restic_peak_rss_kb INTEGER
);

CREATE TABLE run_event (
    run_id       TEXT NOT NULL REFERENCES run (run_id),
    seq          INTEGER NOT NULL,
    occ_id       TEXT,
    outcome      TEXT NOT NULL CHECK (outcome IN (
                     'archived', 'already-archived', 'deferred', 'refused',
                     'retained', 'verified', 'corrupt', 'missing', 'restored',
                     'orphaned', 'queued', 'info')),
    name         TEXT,
    reason       TEXT,
    recorded_at  TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
