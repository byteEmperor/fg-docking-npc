#!/usr/bin/env bash

set -e
set -x

INPUT_CSV="$1"

tail -n +2 "$INPUT_CSV" | while IFS=',' read -r config_yml batch_csv outdir
do
    echo "Running DiffDock with:"
    echo "  config_yml: $config_yml"
    echo "  batch_csv:  $batch_csv"
    echo "  out:    $outdir"

    python -m inference \
        --config "$config_yml" \
        --protein_ligand_csv "$batch_csv" \
        --out_dir "$outdir"

    echo "Finished job"
    echo "-------------------------"
done