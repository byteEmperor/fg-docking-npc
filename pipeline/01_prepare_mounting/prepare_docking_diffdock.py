import csv
import argparse
from pathlib import Path


def build_ligand_path(ligand_path: Path):
    parent = ligand_path.parent.parent.name
    ligand_dir = ligand_path.parent.name
    ligand_file = ligand_path.name
    return f"./to_mount/{parent}/{ligand_dir}/{ligand_file}"


def build_mounted_path(file_path: Path):
    parent = file_path.parent.parent.name
    folder = file_path.parent.name
    filename = file_path.name
    return f"./to_mount/{parent}/{folder}/{filename}"


def generate_batch_csvs(batch_name, protein_name, base_dir):
    yml_dir = base_dir / batch_name / "01_args_yml"
    ligand_dir = base_dir / batch_name / "03_ligands"
    output_dir = base_dir / batch_name / "04_csv"

    protein_path = f"./to_mount/{batch_name}/02_protein/{protein_name}"

    output_dir.mkdir(parents=True, exist_ok=True)

    ligands = [l for l in ligand_dir.rglob("*") if l.is_file()]

    for yml_file in yml_dir.glob("*.yml"):
        csv_path = output_dir / f"{yml_file.stem}.csv"

        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)

            writer.writerow([
                "complex_name",
                "protein_path",
                "ligand_description",
                "protein_sequence"
            ])

            for ligand in ligands:
                ligand_mount = build_ligand_path(ligand)
                complex_name = f"{yml_file.stem}_{ligand.stem}"

                writer.writerow([
                    complex_name,
                    protein_path,
                    ligand_mount,
                    ""
                ])

        print(f"Created {csv_path}")


def generate_control_csv(batch_name, base_dir):
    yml_dir = base_dir / batch_name / "01_args_yml"
    csv_dir = base_dir / batch_name / "04_csv"
    output_file = base_dir / batch_name / "control.csv"

    yml_files = sorted(yml_dir.glob("*.yml"))
    rows = []

    for yml in yml_files:
        stem = yml.stem
        csv_file = csv_dir / f"{stem}.csv"

        if not csv_file.exists():
            print(f"WARNING: No matching CSV for {yml.name}, skipping.")
            continue

        yml_mounted = build_mounted_path(yml)
        csv_mounted = build_mounted_path(csv_file)

        out_dir = f"/workspace/docking/{stem}"

        rows.append([yml_mounted, csv_mounted, out_dir])

    with open(output_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["config_yml", "batch_csv", "outdir"])
        writer.writerows(rows)

    print(f"Created control file: {output_file}")
    print(f"{len(rows)} jobs added.")


def main():
    parser = argparse.ArgumentParser(description="Prepare RunPod docking batch")

    parser.add_argument("batch_name", help="Batch folder name")
    parser.add_argument("protein_name", help="Protein PDB filename")
    parser.add_argument(
        "--base-dir",
        default="/home/nat/Documents/bachelor_project/fg-docking-npc/pipeline/01_prepare_mounting/batches",
        help="Base batches directory"
    )

    args = parser.parse_args()

    base_dir = Path(args.base_dir)

    print("\nGenerating ligand batch CSV files...")
    generate_batch_csvs(args.batch_name, args.protein_name, base_dir)

    print("\nGenerating control.csv...")
    generate_control_csv(args.batch_name, base_dir)

    print("\nDone.")


if __name__ == "__main__":
    main()