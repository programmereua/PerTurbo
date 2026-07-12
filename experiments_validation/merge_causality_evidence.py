#!/usr/bin/env python3
"""
merge_evidence.py  ---  STEP 3 of the Perturbo tool
====================================================
Fuses the two independent causal pipelines into a single, provenance-labelled
evidence file that the web app + agent read directly:

  1. Simcomen / Perturbo  (perturbo_out/perturbo_data.json)
        interventional, model-based: "knocking out gene G is predicted to
        suppress the tumour readout, permutation p = ...".
  2. FCI causal discovery (causal_results/causal_directions_<section>.csv)
        observational, constraint-based: "for stroma->tumour pair (A, B) the
        bootstrap direction is ... with verdict HIGH/MEDIUM/LOW".

The two answer DIFFERENT causal questions, so we never merge them into one
number. Instead each Perturbo target gene is annotated with what the FCI layer
independently says about it, and a single triangulation status is derived:

  corroborated       Perturbo-significant AND FCI finds a stable directed edge
                     for that gene (both lines of evidence agree)  -> strongest.
  simcomen_only      Perturbo-significant but FCI leaves it LOW / undetermined
                     (mechanistic prediction not yet corroborated observationally).
  flag_latent        FCI calls a pair for this gene a shared latent factor (the
                     niche) rather than a direct arrow -> caution to the user.
  fci_only           FCI finds a directed edge but the Perturbo KO is not
                     FDR-significant.
  weak               Neither layer is confident.
  fci_not_evaluated  Gene is outside the FCI panel, so no observational check.

Outputs (originals are left untouched):
  perturbo_out/perturbo_merged.json   <- enriched brain for the app/agent
  perturbo_out/merged_evidence.csv    <- flat, human-readable audit table

Run (anywhere the two result files are reachable):
  python merge_evidence.py
  python merge_evidence.py --perturbo path/to/perturbo_data.json \
                           --causal-dir path/to/causal_results
"""
from __future__ import annotations
import os
import glob
import json
import argparse
import pandas as pd

VERDICT_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "": 0, None: 0}


# --------------------------------------------------------------------------- #
# robust file location + loading
# --------------------------------------------------------------------------- #
def _first_existing(paths):
    for p in paths:
        if p and os.path.isfile(p):
            return p
    return None


def find_perturbo(explicit=None):
    return _first_existing([
        explicit,
        "perturbo_out/perturbo_data.json",
        *sorted(glob.glob("**/perturbo_data.json", recursive=True)),
    ])


def find_causal_csv(section, causal_dir):
    """Locate causal_directions_<section>.csv, being tolerant about where it lives."""
    candidates = [
        os.path.join(causal_dir, f"causal_directions_{section}.csv"),
        os.path.join("causal_results", f"causal_directions_{section}.csv"),
    ]
    candidates += sorted(glob.glob(f"**/causal_directions_{section}.csv", recursive=True))
    return _first_existing(candidates)


def load_fci_table(path):
    """Read an FCI causal_directions CSV into a normalised DataFrame.

    We lower-case the column names and only rely on columns we know the writer
    produces, so a slightly older/newer CSV still merges instead of crashing.
    """
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]

    def col(name, default=None):
        return df[name] if name in df.columns else pd.Series([default] * len(df))

    norm = pd.DataFrame({
        "stroma": col("stroma").astype(str).str.upper(),
        "tumour": col("tumour").astype(str).str.upper(),
        "verdict": col("verdict").astype(str).str.upper(),
        "call": col("call").astype(str),
        "direction_source": col("direction_source", "data").astype(str),
        "ci_tests_agree": col("ci_tests_agree"),
        "correlation": pd.to_numeric(col("correlation"), errors="coerce"),
    })
    return norm


# --------------------------------------------------------------------------- #
# FCI view of a single gene
# --------------------------------------------------------------------------- #
def build_fci_index(fci_df):
    """Return (gene -> list of pair-rows, set-of-all-FCI-genes)."""
    index = {}
    genes = set()
    if fci_df is None:
        return index, genes
    for r in fci_df.itertuples(index=False):
        genes.add(r.stroma)
        genes.add(r.tumour)
        for g in (r.stroma, r.tumour):
            index.setdefault(g, []).append(r)
    return index, genes


def fci_view(gene, index, all_genes):
    """Summarise everything the FCI layer says about `gene`."""
    g = str(gene).upper()
    rows = index.get(g, [])
    if not rows:
        return {
            "evaluated": False,
            "support": "not_evaluated",
            "best_verdict": None,
            "best_call": None,
            "direction_source": None,
            "n_pairs": 0,
            "n_directed": 0,
            "n_latent": 0,
        }

    n_latent = sum(1 for r in rows if "latent" in str(r.call).lower())
    directed = [r for r in rows
                if "->" in str(r.call)
                and VERDICT_RANK.get(str(r.verdict).upper(), 0) >= 2]  # MEDIUM/HIGH
    best = max(rows, key=lambda r: VERDICT_RANK.get(str(r.verdict).upper(), 0))

    if directed:
        support = "directed"
    elif n_latent:
        support = "latent"
    else:
        support = "undetermined"

    return {
        "evaluated": True,
        "support": support,
        "best_verdict": str(best.verdict).upper(),
        "best_call": str(best.call),
        "direction_source": str(getattr(best, "direction_source", "data")),
        "n_pairs": len(rows),
        "n_directed": len(directed),
        "n_latent": int(n_latent),
    }


def triangulate(perturbo_significant, fci):
    """Combine the interventional verdict (Perturbo) and observational verdict (FCI)
    into one honest status label + a plain-language note."""
    support = fci["support"]
    if support == "not_evaluated":
        return "fci_not_evaluated", "Gene is outside the FCI panel; no observational check available."
    if support == "directed":
        if perturbo_significant:
            return "corroborated", (f"Both agree: Perturbo KO is FDR-significant and FCI finds a "
                                    f"stable directed edge ({fci['best_call']}, {fci['best_verdict']}).")
        return "fci_only", (f"FCI finds a directed edge ({fci['best_call']}) but the Perturbo KO "
                            f"is not FDR-significant.")
    if support == "latent":
        return "flag_latent", ("Caution: FCI calls this a shared latent factor (the niche), not a "
                               "direct arrow. The predicted effect may be confounded.")
    # undetermined
    if perturbo_significant:
        return "simcomen_only", ("Mechanistic prediction only: Perturbo KO is FDR-significant but "
                                 "FCI leaves the direction undetermined (expected at Visium spot "
                                 "resolution).")
    return "weak", "Neither layer is confident for this gene."


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #
def merge(perturbo_path, causal_dir):
    with open(perturbo_path) as f:
        data = json.load(f)

    flat_rows = []
    overall = {}

    for section, sec in data.get("sections", {}).items():
        csv_path = find_causal_csv(section, causal_dir)
        fci_df = load_fci_table(csv_path) if csv_path else None
        index, all_genes = build_fci_index(fci_df)
        sec["fci_source"] = os.path.relpath(csv_path) if csv_path else None
        sec["fci_available"] = fci_df is not None

        status_counts = {}
        for pop in sec.get("populations", []):
            for gene_rec in pop.get("top_genes", []):
                gene = gene_rec.get("gene")
                p_sig = bool(gene_rec.get("significant"))
                fci = fci_view(gene, index, all_genes)
                status, note = triangulate(p_sig, fci)

                gene_rec["fci"] = fci
                gene_rec["triangulation"] = {"status": status, "note": note}
                # a priority target that is ALSO observationally corroborated is the
                # single strongest thing the whole tool can say:
                gene_rec["corroborated_priority_target"] = bool(
                    gene_rec.get("priority_target") and status == "corroborated")

                status_counts[status] = status_counts.get(status, 0) + 1
                overall[status] = overall.get(status, 0) + 1
                flat_rows.append({
                    "section": section,
                    "population": pop.get("name"),
                    "gene": gene,
                    "effect": gene_rec.get("effect"),
                    "perturbo_p": gene_rec.get("p_value"),
                    "perturbo_q": gene_rec.get("q_value"),
                    "perturbo_significant": p_sig,
                    "is_hub": gene_rec.get("is_hub"),
                    "priority_target": gene_rec.get("priority_target"),
                    "fci_support": fci["support"],
                    "fci_best_verdict": fci["best_verdict"],
                    "fci_best_call": fci["best_call"],
                    "status": status,
                    "corroborated_priority_target": gene_rec["corroborated_priority_target"],
                    "note": note,
                })
        sec["triangulation_summary"] = status_counts

    data["triangulation_summary"] = overall
    data["merge_note"] = ("Perturbo (interventional, model-based) annotated with FCI "
                          "(observational, constraint-based). Statuses are provenance-labelled; "
                          "the two layers are never collapsed into one score.")
    if "disclaimer" in data:
        data["disclaimer"] += (" Cross-checked against independent observational causal discovery "
                               "(FCI); see each gene's 'triangulation' field.")

    flat = pd.DataFrame(flat_rows)
    # sort so the strongest, corroborated hits float to the top of the audit table
    if not flat.empty:
        rank = {"corroborated": 0, "simcomen_only": 1, "fci_only": 2,
                "flag_latent": 3, "weak": 4, "fci_not_evaluated": 5}
        flat["_r"] = flat["status"].map(rank).fillna(9)
        flat = flat.sort_values(
            ["_r", "corroborated_priority_target", "perturbo_q"],
            ascending=[True, False, True]).drop(columns="_r")
    return data, flat


def print_summary(data, flat):
    print("\n================ merged evidence summary ================")
    for section, sec in data.get("sections", {}).items():
        src = sec.get("fci_source") or "NOT FOUND (all genes marked fci_not_evaluated)"
        print(f"\n  {section}  (FCI source: {src})")
        for status, n in sorted(sec.get("triangulation_summary", {}).items()):
            print(f"      {status:20s} {n}")
    print("\n  OVERALL:")
    for status, n in sorted(data.get("triangulation_summary", {}).items()):
        print(f"      {status:20s} {n}")

    if not flat.empty:
        corr = flat[flat["status"] == "corroborated"]
        print(f"\n  corroborated priority targets (Perturbo-significant AND FCI-directed): "
              f"{int(flat['corroborated_priority_target'].sum())}")
        if not corr.empty:
            cols = ["section", "population", "gene", "perturbo_q", "fci_best_call", "fci_best_verdict"]
            print(corr[cols].head(15).to_string(index=False))
        else:
            print("      (none — expected if FCI returned all LOW/undetermined at spot resolution)")
    print("\n=========================================================")


def main():
    ap = argparse.ArgumentParser(description="Merge Perturbo (interventional) and FCI "
                                             "(observational) causal evidence.")
    ap.add_argument("--perturbo", default=None, help="path to perturbo_data.json")
    ap.add_argument("--causal-dir", default="causal_results",
                    help="folder holding causal_directions_<section>.csv files")
    ap.add_argument("--out", default=None, help="merged JSON output path")
    ap.add_argument("--csv", default=None, help="flat audit CSV output path")
    args = ap.parse_args()

    perturbo_path = find_perturbo(args.perturbo)
    if not perturbo_path:
        raise SystemExit("Could not find perturbo_data.json. Pass it with --perturbo.")
    out_dir = os.path.dirname(perturbo_path) or "."
    out_json = args.out or os.path.join(out_dir, "perturbo_merged.json")
    out_csv = args.csv or os.path.join(out_dir, "merged_evidence.csv")

    print(f"reading Perturbo results : {perturbo_path}")
    print(f"looking for FCI CSVs in  : {args.causal_dir}")

    data, flat = merge(perturbo_path, args.causal_dir)

    with open(out_json, "w") as f:
        json.dump(data, f, indent=2)
    flat.to_csv(out_csv, index=False)

    print_summary(data, flat)
    print(f"\n  wrote {out_json}")
    print(f"  wrote {out_csv}")
    print("\n  The app should now read perturbo_merged.json instead of perturbo_data.json.")


if __name__ == "__main__":
    main()
