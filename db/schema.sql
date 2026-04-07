-- =============================================================================
-- FG-NTR / NPC Docking Pipeline — SQLite Schema
-- =============================================================================
-- Design principles:
--   • Files on disk are the source of truth; this DB is the index + scoreboard.
--   • Append-only: never delete rows, use status flags instead.
--   • All foreign keys enforced (enable with PRAGMA foreign_keys = ON).
--   • parameters_json / value_json cols allow extensibility without migrations.
-- =============================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;   -- safer for concurrent reads during analysis


-- -----------------------------------------------------------------------------
-- 1. STRUCTURES
--    The biological inputs: receptor, FG-NTR peptide, or pre-assembled complex
--    used as starting material for a docking run.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS structures (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,   -- e.g. 'Nup98_FxFG_receptor'
    pdb_path        TEXT    NOT NULL,
    molecule_type   TEXT,                      -- 'receptor', 'ligand', 'complex'
    chain_fg        TEXT,                      -- FG-NTR chain identifier
    chain_ntr       TEXT,                      -- NTR chain identifier
    sequence        TEXT,                      -- optional, for quick reference
    notes           TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);


-- -----------------------------------------------------------------------------
-- 2. DOCKING RUNS
--    One row per invocation of a docking tool (DiffDock, gnina, SurfDock…).
--    Captures the exact parameters so results are always reproducible.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS docking_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    structure_id    INTEGER NOT NULL REFERENCES structures(id),
    tool            TEXT    NOT NULL,   -- 'diffdock' | 'gnina' | 'surfdock'
    tool_version    TEXT,               -- e.g. '1.1.0'
    run_label       TEXT,               -- human-readable, e.g. 'diffdock_nup98_run01'
    output_dir      TEXT    NOT NULL,
    parameters_json TEXT,               -- full CLI args / config as JSON
    status          TEXT    NOT NULL DEFAULT 'running',
                                        -- 'running' | 'done' | 'failed'
    started_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_structure ON docking_runs(structure_id);
CREATE INDEX IF NOT EXISTS idx_runs_tool       ON docking_runs(tool);


-- -----------------------------------------------------------------------------
-- 3. POSES
--    One row per docking output pose (the logical entity — tool-native rank/
--    confidence score). Physical files that represent this pose live in FILES.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS poses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES docking_runs(id),
    rank            INTEGER,            -- tool-assigned rank (1 = best)
    confidence      REAL,               -- tool-native confidence / score
    status          TEXT    NOT NULL DEFAULT 'raw',
                                        -- 'raw' | 'converted' | 'postprocessed'
                                        -- | 'failed' | 'excluded'
    exclusion_reason TEXT,              -- why it was excluded, if status='excluded'
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_poses_run    ON poses(run_id);
CREATE INDEX IF NOT EXISTS idx_poses_status ON poses(status);


-- -----------------------------------------------------------------------------
-- 4. FILES
--    Every file that exists in the pipeline gets a row here.
--    Covers: raw SDF/PDB from docking, converted PDBs, complexes, renumbered
--    files, receptor copies, etc.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path            TEXT    NOT NULL UNIQUE,
    file_type       TEXT    NOT NULL,   -- 'sdf' | 'pdb' | 'mol2' | ...
    role            TEXT    NOT NULL,   -- see roles below:
                                        --   'raw_pose'          – straight from docking tool
                                        --   'converted_pose'    – e.g. SDF→PDB
                                        --   'complex'           – receptor + pose merged
                                        --   'plip_ready'        – renumbered for PLIP
                                        --   'haddock_ready'     – prepared for HADDOCK
                                        --   'input_structure'   – original receptor/ligand
    pose_id         INTEGER REFERENCES poses(id),   -- NULL for input structures
    exists_on_disk  INTEGER NOT NULL DEFAULT 1,      -- 0 if file was deleted/moved
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_files_pose ON files(pose_id);
CREATE INDEX IF NOT EXISTS idx_files_role ON files(role);


-- -----------------------------------------------------------------------------
-- 5. TRANSFORMATIONS
--    Directed edges in the file graph: input_file → operation → output_file.
--    Use multiple rows with the same output_file_id to model merges
--    (e.g. receptor + pose → complex uses two input rows).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transformations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    operation       TEXT    NOT NULL,   -- 'sdf_to_pdb' | 'build_complex'
                                        -- | 'renumber' | 'strip_solvent'
                                        -- | 'extract_chain' | ...
    input_file_id   INTEGER NOT NULL REFERENCES files(id),
    output_file_id  INTEGER NOT NULL REFERENCES files(id),
    parameters_json TEXT,               -- any relevant options used
    status          TEXT    NOT NULL DEFAULT 'ok',
                                        -- 'ok' | 'failed' | 'skipped'
    notes           TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tx_input  ON transformations(input_file_id);
CREATE INDEX IF NOT EXISTS idx_tx_output ON transformations(output_file_id);


-- -----------------------------------------------------------------------------
-- 6. SCORES
--    Entity-Attribute-Value table: one row per (pose, tool, metric).
--    Adding a new scoring tool never requires a schema change.
--    Complex PLIP output (e.g. full interaction dict) goes in value_json.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pose_id         INTEGER NOT NULL REFERENCES poses(id),
    scored_file_id  INTEGER REFERENCES files(id),  -- which physical file was scored
    tool            TEXT    NOT NULL,   -- 'haddock_em' | 'haddock_md' | 'gnina'
                                        -- | 'plip' | 'aromatic'
    score_type      TEXT    NOT NULL,   -- 'haddock_score' | 'cnn_score'
                                        -- | 'cnn_affinity' | 'vina_score'
                                        -- | 'aromatic_contacts' | 'hbond_count'
                                        -- | 'hydrophobic_contacts' | ...
    value_float     REAL,               -- use for any single numeric score
    value_json      TEXT,               -- use for structured output (PLIP interactions)
    scored_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_scores_pose       ON scores(pose_id);
CREATE INDEX IF NOT EXISTS idx_scores_tool_type  ON scores(tool, score_type);


-- =============================================================================
-- CONVENIENCE VIEWS
-- =============================================================================

-- Flat summary: one row per pose with all scores pivoted as columns.
-- Extend the CASE blocks as you add new scoring tools/types.
CREATE VIEW IF NOT EXISTS v_pose_summary AS
SELECT
    p.id                                                    AS pose_id,
    p.rank,
    p.confidence                                            AS tool_confidence,
    p.status                                                AS pose_status,
    dr.tool                                                 AS docking_tool,
    dr.run_label,
    s.name                                                  AS structure_name,

    -- Scores (NULL when not yet computed)
    MAX(CASE WHEN sc.score_type = 'haddock_score'        THEN sc.value_float END) AS haddock_em,
    MAX(CASE WHEN sc.score_type = 'haddock_md_score'     THEN sc.value_float END) AS haddock_md,
    MAX(CASE WHEN sc.score_type = 'cnn_score'            THEN sc.value_float END) AS gnina_cnn,
    MAX(CASE WHEN sc.score_type = 'cnn_affinity'         THEN sc.value_float END) AS gnina_affinity,
    MAX(CASE WHEN sc.score_type = 'vina_score'           THEN sc.value_float END) AS vina,
    MAX(CASE WHEN sc.score_type = 'aromatic_contacts'    THEN sc.value_float END) AS aromatic_contacts,
    MAX(CASE WHEN sc.score_type = 'hbond_count'          THEN sc.value_float END) AS hbonds,
    MAX(CASE WHEN sc.score_type = 'hydrophobic_contacts' THEN sc.value_float END) AS hydrophobic,

    -- File paths for the most useful physical representations
    raw.path                                                AS raw_file,
    converted.path                                          AS converted_pdb,
    complex.path                                            AS complex_pdb,
    plip.path                                               AS plip_ready_pdb

FROM poses p
JOIN docking_runs  dr       ON dr.id           = p.run_id
JOIN structures    s        ON s.id            = dr.structure_id
LEFT JOIN scores   sc       ON sc.pose_id      = p.id
LEFT JOIN files    raw      ON raw.pose_id     = p.id AND raw.role      = 'raw_pose'
LEFT JOIN files    converted ON converted.pose_id = p.id AND converted.role = 'converted_pose'
LEFT JOIN files    complex  ON complex.pose_id = p.id AND complex.role  = 'complex'
LEFT JOIN files    plip     ON plip.pose_id    = p.id AND plip.role     = 'plip_ready'

GROUP BY p.id;


-- File lineage: show every file and its immediate parent (if any).
CREATE VIEW IF NOT EXISTS v_file_lineage AS
SELECT
    f_out.id                AS file_id,
    f_out.path              AS file_path,
    f_out.role              AS file_role,
    t.operation,
    f_in.id                 AS parent_file_id,
    f_in.path               AS parent_path,
    f_in.role               AS parent_role
FROM files f_out
LEFT JOIN transformations t   ON t.output_file_id = f_out.id
LEFT JOIN files           f_in ON f_in.id          = t.input_file_id;


-- Run overview: how many poses per run, how many scored.
CREATE VIEW IF NOT EXISTS v_run_overview AS
SELECT
    dr.id               AS run_id,
    dr.run_label,
    dr.tool,
    dr.status,
    s.name              AS structure_name,
    COUNT(DISTINCT p.id)                                        AS total_poses,
    COUNT(DISTINCT CASE WHEN p.status = 'postprocessed'
                        THEN p.id END)                          AS postprocessed,
    COUNT(DISTINCT CASE WHEN p.status = 'excluded'
                        THEN p.id END)                          AS excluded,
    COUNT(DISTINCT sc.pose_id)                                  AS scored_poses,
    dr.started_at,
    dr.finished_at
FROM docking_runs dr
JOIN structures   s  ON s.id  = dr.structure_id
LEFT JOIN poses   p  ON p.run_id = dr.id
LEFT JOIN scores  sc ON sc.pose_id = p.id
GROUP BY dr.id;