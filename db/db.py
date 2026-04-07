"""
db.py — Shared database API for the NPC docking pipeline.

Every pipeline script imports from here. No raw SQL outside this file.

Usage:
    from db.db import register_structure, register_run, register_pose, ...
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

DB_PATH = Path(__file__).parent.parent / "pipeline.db"


# =============================================================================
# Connection
# =============================================================================

def get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# =============================================================================
# Structures
# =============================================================================

def register_structure(
    name: str,
    pdb_path: Union[str, Path],
    molecule_type: str = "complex",
    chain_fg: Optional[str] = None,
    chain_ntr: Optional[str] = None,
    sequence: Optional[str] = None,
    notes: str = "",
) -> int:
    """
    Register an input structure. Returns the row id.
    Safe to call multiple times — INSERT OR IGNORE means duplicates are skipped.
    """
    with get_conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO structures
                (name, pdb_path, molecule_type, chain_fg, chain_ntr, sequence, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (name, str(pdb_path), molecule_type, chain_fg, chain_ntr, sequence, notes))
        row = conn.execute(
            "SELECT id FROM structures WHERE name = ?", (name,)
        ).fetchone()
        return row["id"]


def get_structure(name: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM structures WHERE name = ?", (name,)
        ).fetchone()


# =============================================================================
# Docking runs
# =============================================================================

def register_run(
    structure_id: int,
    tool: str,
    output_dir: Union[str, Path],
    run_label: Optional[str] = None,
    tool_version: Optional[str] = None,
    parameters: Optional[dict] = None,
) -> int:
    """
    Register a new docking run (status = 'running').
    Returns the run id.
    """
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO docking_runs
                (structure_id, tool, tool_version, run_label,
                 output_dir, parameters_json, status, started_at)
            VALUES (?, ?, ?, ?, ?, ?, 'running', ?)
        """, (
            structure_id, tool, tool_version, run_label,
            str(output_dir), json.dumps(parameters or {}), _now()
        ))
        return cur.lastrowid


def finish_run(run_id: int, status: str = "done") -> None:
    """Mark a run as done (or failed)."""
    assert status in ("done", "failed")
    with get_conn() as conn:
        conn.execute("""
            UPDATE docking_runs SET status = ?, finished_at = ? WHERE id = ?
        """, (status, _now(), run_id))


# =============================================================================
# Poses
# =============================================================================

def register_pose(
    run_id: int,
    rank: Optional[int] = None,
    confidence: Optional[float] = None,
) -> int:
    """
    Register a logical pose (tool output rank). Returns the pose id.
    Physical files are registered separately via register_file().
    """
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO poses (run_id, rank, confidence, status)
            VALUES (?, ?, ?, 'raw')
        """, (run_id, rank, confidence))
        return cur.lastrowid


def update_pose_status(
    pose_id: int,
    status: str,
    exclusion_reason: Optional[str] = None,
) -> None:
    """
    Valid statuses: 'raw' | 'converted' | 'postprocessed' | 'failed' | 'excluded'
    """
    with get_conn() as conn:
        conn.execute("""
            UPDATE poses SET status = ?, exclusion_reason = ? WHERE id = ?
        """, (status, exclusion_reason, pose_id))


# =============================================================================
# Files
# =============================================================================

def register_file(
    path: Union[str, Path],
    file_type: str,
    role: str,
    pose_id: Optional[int] = None,
) -> int:
    """
    Register a file in the pipeline. Returns the file id.
    INSERT OR IGNORE — safe to call repeatedly for the same path.

    Roles:
        'raw_pose'          straight from docking tool
        'converted_pose'    e.g. SDF → PDB
        'complex'           receptor + pose merged into one file
        'plip_ready'        renumbered for PLIP
        'haddock_ready'     prepared for HADDOCK
        'input_structure'   original receptor / ligand
    """
    with get_conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO files (path, file_type, role, pose_id)
            VALUES (?, ?, ?, ?)
        """, (str(path), file_type, role, pose_id))
        row = conn.execute(
            "SELECT id FROM files WHERE path = ?", (str(path),)
        ).fetchone()
        return row["id"]


def get_file(path: Union[str, Path]) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM files WHERE path = ?", (str(path),)
        ).fetchone()


def mark_file_missing(path: Union[str, Path]) -> None:
    """Call when a file is deleted or moved off disk."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE files SET exists_on_disk = 0 WHERE path = ?", (str(path),)
        )


# =============================================================================
# Transformations
# =============================================================================

def register_transformation(
    operation: str,
    input_file_ids: Union[int, list[int]],
    output_file_id: int,
    parameters: Optional[dict] = None,
    status: str = "ok",
    notes: str = "",
) -> None:
    """
    Record that one or more input files were transformed into an output file.

    For merges (e.g. build_complex from receptor + pose), pass a list:
        register_transformation("build_complex", [receptor_id, pose_id], complex_id)
    """
    if isinstance(input_file_ids, int):
        input_file_ids = [input_file_ids]

    params_json = json.dumps(parameters or {})
    with get_conn() as conn:
        for input_id in input_file_ids:
            conn.execute("""
                INSERT INTO transformations
                    (operation, input_file_id, output_file_id,
                     parameters_json, status, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (operation, input_id, output_file_id,
                  params_json, status, notes, _now()))


# =============================================================================
# Scores
# =============================================================================

def register_score(
    pose_id: int,
    tool: str,
    score_type: str,
    value_float: Optional[float] = None,
    value_json: Optional[dict] = None,
    scored_file_id: Optional[int] = None,
) -> None:
    """
    Register a single scoring result.

    For numeric scores (most cases):
        register_score(pose_id, 'haddock_em', 'haddock_score', value_float=-250.3)

    For rich PLIP output:
        register_score(pose_id, 'plip', 'interactions', value_json=plip_dict)

    scored_file_id: which physical file was actually passed to the scorer.
    """
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO scores
                (pose_id, scored_file_id, tool, score_type,
                 value_float, value_json, scored_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            pose_id, scored_file_id, tool, score_type,
            value_float,
            json.dumps(value_json) if value_json is not None else None,
            _now()
        ))


def register_scores_bulk(rows: list[dict]) -> None:
    """
    Insert multiple score rows in one transaction. Each dict should have keys:
    pose_id, tool, score_type, and at least one of value_float / value_json.
    Optional: scored_file_id.
    """
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO scores
                (pose_id, scored_file_id, tool, score_type,
                 value_float, value_json, scored_at)
            VALUES (:pose_id, :scored_file_id, :tool, :score_type,
                    :value_float, :value_json, :scored_at)
        """, [
            {
                "pose_id":        r["pose_id"],
                "scored_file_id": r.get("scored_file_id"),
                "tool":           r["tool"],
                "score_type":     r["score_type"],
                "value_float":    r.get("value_float"),
                "value_json":     json.dumps(r["value_json"])
                                  if r.get("value_json") is not None else None,
                "scored_at":      _now(),
            }
            for r in rows
        ])


# =============================================================================
# Lineage queries
# =============================================================================

def get_lineage(path: Union[str, Path]) -> list[sqlite3.Row]:
    """
    Walk backwards up the transformation chain from a given file.
    Returns rows ordered from root ancestor → this file.
    """
    with get_conn() as conn:
        return conn.execute("""
            WITH RECURSIVE lineage(file_id, path, role, operation, depth) AS (
                SELECT f.id, f.path, f.role, NULL, 0
                FROM files f WHERE f.path = ?

                UNION ALL

                SELECT f.id, f.path, f.role, t.operation, l.depth + 1
                FROM lineage l
                JOIN transformations t ON t.output_file_id = l.file_id
                JOIN files f           ON f.id = t.input_file_id
            )
            SELECT * FROM lineage ORDER BY depth DESC
        """, (str(path),)).fetchall()


def get_descendants(path: Union[str, Path]) -> list[sqlite3.Row]:
    """
    Walk forwards down the transformation chain from a given file.
    """
    with get_conn() as conn:
        return conn.execute("""
            WITH RECURSIVE descendants(file_id, path, role, operation) AS (
                SELECT f.id, f.path, f.role, NULL
                FROM files f WHERE f.path = ?

                UNION ALL

                SELECT f.id, f.path, f.role, t.operation
                FROM descendants d
                JOIN transformations t ON t.input_file_id = d.file_id
                JOIN files f           ON f.id = t.output_file_id
            )
            SELECT * FROM descendants
        """, (str(path),)).fetchall()


def get_pose_files(pose_id: int) -> list[sqlite3.Row]:
    """Return all files registered to a pose, ordered by creation time."""
    with get_conn() as conn:
        return conn.execute("""
            SELECT * FROM files WHERE pose_id = ? ORDER BY created_at
        """, (pose_id,)).fetchall()


def get_pose_scores(pose_id: int) -> list[sqlite3.Row]:
    """Return all scores for a pose."""
    with get_conn() as conn:
        return conn.execute("""
            SELECT * FROM scores WHERE pose_id = ? ORDER BY tool, score_type
        """, (pose_id,)).fetchall()