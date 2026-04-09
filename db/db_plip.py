"""
db_plip.py — PLIP-specific database helpers.

Handles parsing PLIP output and inserting rows into plip_contacts.
PLIP can output XML (via the Python API) or JSON (via plip-tool CLI flags).
Both formats are supported here.

Usage:
    from db.db_plip import register_plip_contacts
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from db.db import get_conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# =============================================================================
# Main entry point
# =============================================================================

def register_plip_contacts(
    pose_id:        int,
    scored_file_id: int,
    plip_output:    dict,
    resid_map:      Optional[dict[int, int]] = None,
) -> int:
    """
    Parse a PLIP output dict and insert one row per interaction into
    plip_contacts. Returns the number of contacts inserted.

    Args:
        pose_id:        DB id of the pose being scored.
        scored_file_id: DB id of the PDB file passed to PLIP (plip_ready).
        plip_output:    PLIP output as a Python dict.
                        Expected to follow the structure produced by
                        parse_plip_xml() or parse_plip_json() below.
        resid_map:      Optional dict mapping renumbered resid → original resid,
                        e.g. {1: 47, 2: 48, ...}.
                        Used to populate *_resid_original columns.

    PLIP output structure expected:
        {
          "interactions": [
            {
              "type":         "pistacking",     # interaction type
              "rec_chain":    "A",
              "rec_resname":  "PHE",
              "rec_resid":    12,
              "rec_atom":     "CG",             # optional
              "lig_chain":    "C",
              "lig_resname":  "PHE",
              "lig_resid":    3,
              "lig_atom":     "CG",             # optional
              "distance":     3.82,
              "angle":        14.3,             # optional
              "is_donor_rec": null,             # optional, for hbonds
              "sidechain":    1,                # optional
              "raw":          { ... }           # full PLIP block
            },
            ...
          ]
        }
    """
    interactions = plip_output.get("interactions", [])
    if not interactions:
        return 0

    rows = []
    for contact in interactions:
        rec_resid = contact.get("rec_resid")
        lig_resid = contact.get("lig_resid")

        rows.append({
            "pose_id":          pose_id,
            "scored_file_id":   scored_file_id,
            "rec_chain":        contact.get("rec_chain"),
            "rec_resname":      contact.get("rec_resname"),
            "rec_resid":        rec_resid,
            "rec_resid_original": resid_map.get(rec_resid) if resid_map and rec_resid else rec_resid,
            "rec_atom":         contact.get("rec_atom"),
            "lig_chain":        contact.get("lig_chain"),
            "lig_resname":      contact.get("lig_resname"),
            "lig_resid":        lig_resid,
            "lig_resid_original": resid_map.get(lig_resid) if resid_map and lig_resid else lig_resid,
            "lig_atom":         contact.get("lig_atom"),
            "interaction_type": contact["type"],
            "distance":         contact.get("distance"),
            "angle":            contact.get("angle"),
            "is_donor_rec":     contact.get("is_donor_rec"),
            "sidechain":        contact.get("sidechain"),
            "raw_json":         json.dumps(contact.get("raw")) if contact.get("raw") else None,
            "created_at":       _now(),
        })

    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO plip_contacts (
                pose_id, scored_file_id,
                rec_chain, rec_resname, rec_resid, rec_resid_original, rec_atom,
                lig_chain, lig_resname, lig_resid, lig_resid_original, lig_atom,
                interaction_type, distance, angle, is_donor_rec, sidechain,
                raw_json, created_at
            ) VALUES (
                :pose_id, :scored_file_id,
                :rec_chain, :rec_resname, :rec_resid, :rec_resid_original, :rec_atom,
                :lig_chain, :lig_resname, :lig_resid, :lig_resid_original, :lig_atom,
                :interaction_type, :distance, :angle, :is_donor_rec, :sidechain,
                :raw_json, :created_at
            )
        """, rows)

    return len(rows)


# =============================================================================
# PLIP output parsers
# =============================================================================

def parse_plip_xml(xml_path: str | Path) -> dict:
    """
    Parse a PLIP XML report into the canonical dict format expected by
    register_plip_contacts().

    PLIP XML is produced by the Python API:
        from plip.structure.preparation import PDBComplex
        mol = PDBComplex()
        mol.load_pdb(str(pdb_path))
        mol.analyze()
        # Then iterate mol.interaction_sets for each binding site

    This parser handles the XML report file written by:
        plip -f complex.pdb -x -o output_dir/
    """
    import xml.etree.ElementTree as ET

    tree = ET.parse(xml_path)
    root = tree.getroot()
    contacts = []

    # Map from PLIP XML tag names to our canonical type strings
    TYPE_MAP = {
        "hydrophobic_interaction": "hydrophobic",
        "hydrogen_bond":           "hbond",
        "water_bridge":            "waterbridge",
        "salt_bridge":             "saltbridge",
        "pi_stacking":             "pistacking",
        "pi_cation_interaction":   "pication",
        "halogen_bond":            "halogen",
        "metal_complex":           "metal",
    }

    def _text(el, tag, default=None):
        child = el.find(tag)
        return child.text.strip() if child is not None and child.text else default

    def _float(el, tag):
        val = _text(el, tag)
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    def _int(el, tag):
        val = _text(el, tag)
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    for bs in root.findall(".//bindingsite"):
        for plip_tag, canon_type in TYPE_MAP.items():
            for interaction in bs.findall(f".//{plip_tag}"):
                contact = {
                    "type": canon_type,

                    # Receptor side
                    "rec_chain":   _text(interaction, "resnr_lig") and _text(interaction, "reschain"),
                    "rec_resname": _text(interaction, "restype"),
                    "rec_resid":   _int(interaction,  "resnr"),
                    "rec_atom":    _text(interaction, "protcarbonidx") or _text(interaction, "donoridx"),

                    # Ligand side
                    "lig_chain":   _text(interaction, "reschain_lig"),
                    "lig_resname": _text(interaction, "restype_lig"),
                    "lig_resid":   _int(interaction,  "resnr_lig"),
                    "lig_atom":    _text(interaction, "ligatom") or _text(interaction, "acceptoridx"),

                    # Geometry
                    "distance":    _float(interaction, "dist")
                                   or _float(interaction, "dist_h-a")
                                   or _float(interaction, "dist_d-a"),
                    "angle":       _float(interaction, "angle"),
                    "is_donor_rec":int(_text(interaction, "protisdon", "0") == "True"),
                    "sidechain":   int(_text(interaction, "sidechain", "0") == "True"),

                    "raw": {child.tag: child.text for child in interaction},
                }
                contacts.append(contact)

    return {"interactions": contacts}


def parse_plip_json(json_path: str | Path) -> dict:
    """
    Parse a PLIP JSON report. If you call PLIP's Python API directly
    and serialise the interaction objects yourself, normalise them here.

    Assumes the JSON has structure:
        {
          "binding_sites": {
            "LIG:C:1": {
              "hydrophobic": [ {"resnr": 12, "restype": "PHE", ...}, ... ],
              "hbond":       [ ... ],
              ...
            }
          }
        }
    Adjust the field names to match your actual PLIP JSON output.
    """
    with open(json_path) as f:
        raw = json.load(f)

    PLIP_KEY_TO_TYPE = {
        "hydrophobic":  "hydrophobic",
        "hbond":        "hbond",
        "waterbridge":  "waterbridge",
        "saltbridge":   "saltbridge",
        "pistacking":   "pistacking",
        "pication":     "pication",
        "halogen":      "halogen",
        "metal":        "metal",
    }

    contacts = []
    for site_key, site in raw.get("binding_sites", {}).items():
        for plip_key, canon_type in PLIP_KEY_TO_TYPE.items():
            for interaction in site.get(plip_key, []):
                contact = {
                    "type":        canon_type,
                    "rec_chain":   interaction.get("reschain"),
                    "rec_resname": interaction.get("restype"),
                    "rec_resid":   interaction.get("resnr"),
                    "rec_atom":    interaction.get("protcarbonidx") or interaction.get("donoridx"),
                    "lig_chain":   interaction.get("reschain_lig"),
                    "lig_resname": interaction.get("restype_lig"),
                    "lig_resid":   interaction.get("resnr_lig"),
                    "lig_atom":    interaction.get("ligatom") or interaction.get("acceptoridx"),
                    "distance":    interaction.get("dist") or interaction.get("dist_h-a"),
                    "angle":       interaction.get("angle"),
                    "is_donor_rec":int(interaction.get("protisdon", False)),
                    "sidechain":   int(interaction.get("sidechain", False)),
                    "raw":         interaction,
                }
                contacts.append(contact)

    return {"interactions": contacts}


# =============================================================================
# Convenience query helpers
# =============================================================================

def get_contacts_for_pose(pose_id: int) -> list[sqlite3.Row]:
    """Return all PLIP contacts for a pose."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM plip_contacts WHERE pose_id = ? ORDER BY interaction_type, distance",
            (pose_id,)
        ).fetchall()


def get_pistacking_contacts(run_id: Optional[int] = None) -> list[sqlite3.Row]:
    """
    Return all π–π stacking contacts, optionally filtered to a single run.
    These are the biologically key interactions for FG-NTR docking.
    """
    with get_conn() as conn:
        if run_id:
            return conn.execute("""
                SELECT pc.*, p.rank, p.confidence
                FROM plip_contacts pc
                JOIN poses p ON p.id = pc.pose_id
                WHERE pc.interaction_type = 'pistacking'
                  AND p.run_id = ?
                ORDER BY pc.distance
            """, (run_id,)).fetchall()
        else:
            return conn.execute("""
                SELECT pc.*, p.rank, p.confidence, dr.tool, dr.run_label
                FROM plip_contacts pc
                JOIN poses        p  ON p.id  = pc.pose_id
                JOIN docking_runs dr ON dr.id = p.run_id
                WHERE pc.interaction_type = 'pistacking'
                ORDER BY dr.tool, p.rank, pc.distance
            """).fetchall()


def get_hotspot_residues(
    min_pose_count: int = 5,
    interaction_type: Optional[str] = None,
) -> list[sqlite3.Row]:
    """
    Return receptor residues contacted in at least `min_pose_count` poses.
    Useful for identifying binding hotspots.
    """
    type_filter = "AND interaction_type = ?" if interaction_type else ""
    params = [min_pose_count]
    if interaction_type:
        params.insert(0, interaction_type)

    with get_conn() as conn:
        return conn.execute(f"""
            SELECT
                rec_chain, rec_resname, rec_resid_original,
                interaction_type,
                COUNT(DISTINCT pose_id) AS pose_count,
                AVG(distance)           AS avg_distance,
                MIN(distance)           AS min_distance
            FROM plip_contacts
            WHERE 1=1 {type_filter}
            GROUP BY rec_resid_original, rec_chain, interaction_type
            HAVING pose_count >= ?
            ORDER BY pose_count DESC
        """, params).fetchall()