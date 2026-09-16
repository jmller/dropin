PRAGMA foreign_keys = ON;

CREATE TABLE restore_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    protocol_version INTEGER NOT NULL CHECK (protocol_version = 1),
    generation TEXT NOT NULL UNIQUE CHECK (length(generation) = 32),
    created_at TEXT NOT NULL,
    activation TEXT NOT NULL CHECK (activation IN ('blocked', 'enabled')),
    blocked_gates_json TEXT NOT NULL
);

CREATE TABLE restore_destination (
    destination_id TEXT PRIMARY KEY CHECK (length(destination_id) = 32),
    exact_path TEXT NOT NULL UNIQUE,
    binding_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    active_item_id TEXT NULL REFERENCES restore_item(item_id)
        DEFERRABLE INITIALLY DEFERRED,
    UNIQUE (destination_id, exact_path)
);

CREATE TABLE restore_request (
    request_id TEXT PRIMARY KEY,
    token TEXT NOT NULL UNIQUE CHECK (length(token) = 32),
    destination_id TEXT NOT NULL,
    destination_path TEXT NOT NULL,
    force INTEGER NOT NULL CHECK (force IN (0, 1)),
    policy_version INTEGER NOT NULL CHECK (policy_version = 1),
    created_at TEXT NOT NULL,
    UNIQUE (request_id, destination_id),
    FOREIGN KEY (destination_id, destination_path)
        REFERENCES restore_destination(destination_id, exact_path)
);

CREATE TABLE restore_item (
    item_id TEXT PRIMARY KEY CHECK (length(item_id) = 32),
    request_id TEXT NOT NULL REFERENCES restore_request(request_id),
    destination_id TEXT NOT NULL REFERENCES restore_destination(destination_id),
    origin_store_id TEXT NOT NULL,
    occurrence_id TEXT NOT NULL,
    selected_rel_path TEXT NOT NULL,
    confirmed_attempt_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    repository TEXT NOT NULL,
    export_digest TEXT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'dir', 'bundle', 'symlink')),
    final_name TEXT NOT NULL,
    entry_count INTEGER NOT NULL CHECK (entry_count > 0),
    root_digest TEXT NOT NULL,
    contract_digest TEXT NOT NULL,
    source_binding_json TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN (
        'INTENT', 'VERIFIED', 'ASIDE_INTENT', 'ASIDE_DONE',
        'PUBLISH_INTENT', 'PUBLISH_DONE', 'ABORT',
        'ROLLBACK_INTENT', 'ROLLED_BACK', 'TERMINAL')),
    disposition TEXT NOT NULL CHECK (disposition IN (
        'active', 'completed', 'failed', 'cancelled', 'relinquished')),
    attempt INTEGER NOT NULL CHECK (attempt >= 1 AND attempt < 9223372036854775807),
    transition INTEGER NOT NULL CHECK (transition >= 1 AND transition < 9223372036854775807),
    verification_completed INTEGER NOT NULL CHECK (verification_completed IN (0, 1)),
    publication TEXT NOT NULL CHECK (publication IN (
        'not_started', 'not_moved', 'established', 'uncertain')),
    old_entry TEXT NOT NULL CHECK (old_entry IN (
        'absent', 'untouched', 'aside', 'returned', 'uncertain')),
    stage_label TEXT NOT NULL,
    aside_label TEXT NULL,
    source_identity_json TEXT NULL,
    primary_error_json TEXT NULL,
    secondary_errors_json TEXT NOT NULL DEFAULT '[]',
    result_code INTEGER NULL CHECK (result_code IN (0, 1, 2, 3, 4)),
    receipt_json TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (request_id, occurrence_id, selected_rel_path),
    UNIQUE (request_id, item_id),
    FOREIGN KEY (request_id, destination_id)
        REFERENCES restore_request(request_id, destination_id),
    CHECK ((phase = 'TERMINAL') = (receipt_json IS NOT NULL)),
    CHECK ((phase = 'TERMINAL') = (disposition != 'active')),
    CHECK ((phase = 'TERMINAL') = (result_code IS NOT NULL))
);

CREATE TABLE restore_alias (
    request_id TEXT NOT NULL,
    literal_path TEXT NOT NULL,
    item_id TEXT NOT NULL,
    PRIMARY KEY (request_id, literal_path),
    FOREIGN KEY (request_id, item_id)
        REFERENCES restore_item(request_id, item_id)
);

CREATE TABLE restore_attempt (
    item_id TEXT NOT NULL REFERENCES restore_item(item_id),
    number INTEGER NOT NULL CHECK (number >= 1 AND number < 9223372036854775807),
    stage_label TEXT NOT NULL UNIQUE,
    aside_label TEXT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN (
        'active', 'verified', 'published', 'abandoned', 'rolled_back', 'retained')),
    source_identity_json TEXT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (item_id, number)
);

CREATE TABLE restore_transition (
    item_id TEXT NOT NULL REFERENCES restore_item(item_id),
    number INTEGER NOT NULL CHECK (number >= 1 AND number < 9223372036854775807),
    phase TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (item_id, number)
);

CREATE UNIQUE INDEX restore_destination_active_item
    ON restore_destination(active_item_id) WHERE active_item_id IS NOT NULL;
CREATE INDEX restore_item_request ON restore_item(request_id, item_id);
CREATE INDEX restore_item_phase ON restore_item(phase, disposition);

PRAGMA user_version = 1;
