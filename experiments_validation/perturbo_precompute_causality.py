#!/usr/bin/env python3
"""
perturbo_precompute.py  ---  STEP 1 of the Perturbo tool
=========================================================
Runs the Perturbo engine ONCE on each tumour section and saves everything the
web app + agent will need as a single JSON file (+ spatial map PNGs). The live
demo then just READS these files -- nothing heavy runs during judging.

What it computes (the "target dossier"), per section:
  DISCOVER:
    - rank cell POPULATIONS (RCTD first_type) by predicted tumour-suppressive effect
    - within each population, rank its DRIVER GENES by predicted effect
    - a spatial MAP showing where the knockout effect lands
    - a CONFIDENCE number (driver-vs-random specificity)

Reuses your validated machinery from celcomen_dose_response.py / celltype work:
  prep, unit_norm, train_sparse, relax, normalize_g2g_sparse_signed.

Output:
  perturbo_out/perturbo_data.json      <- the tool's "brain" (app + agent read this)
  perturbo_out/maps/<section>_<pop>.png <- spatial maps

Run (on your desktop, in the celcomen env):
  python perturbo_precompute.py
"""
import os, json, tempfile, warnings
import numpy as np, pandas as pd, scanpy as sc, torch
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import TwoSlopeNorm
from sklearn.neighbors import kneighbors_graph
from scipy.sparse import csr_matrix, issparse
warnings.filterwarnings("ignore")

from celcomen.models.celcomen import celcomen
from celcomen.models.simcomen import simcomen
from celcomen.datareaders.datareader import get_dataset_loaders
from celcomen.utils.helpers import normalize_g2g, calc_sphex
import celcomen.training_plan.train as T

# ---------------- config ----------------
SECTIONS = {
    "HM11": ("handoff_to_eva/data/IU_PDA_HM11.h5ad",
             "handoff_to_eva/drivers/IU_PDA_HM11_drivers_per_celltype.csv", "liver metastasis"),
    "T11":  ("handoff_to_eva/data/IU_PDA_T11.h5ad",
             "handoff_to_eva/drivers/IU_PDA_T11_drivers_per_celltype.csv",  "primary tumour"),
}
CELLTYPE_COL, THEME_COL, SAMPLE_ID = "first_type", "theme", "sample_id"
TUMOUR_THEMES = {"tumour/metabolic", "tumour_metabolic", "tumour", "tumor"}
K, SEED, CC_EPOCHS, CC_LR, ZMFT, SCM_STEPS, TOP = 6, 0, 200, 1e-1, 1e-1, 80, 25
THRESHOLD = 0.15
N_TOP_GENES = 8          # top driver genes to report per population

# --- statistical evaluation (permutation null + FDR) ---------------------------
# The raw specificity ratio (|driver KO| / |one random KO|) is a single draw and
# gives no p-value. These turn each knockout effect into an empirical permutation
# p-value: we KO random gene sets of the SAME size in the SAME spots many times,
# build a null distribution of the tumour response magnitude, and ask how often
# random matches or beats the observed driver effect. This is the same permutation
# logic used in the Celcomen cross-block validation. COST: each draw is a full
# `relax`, so a section runs ~N_PERM extra relaxes per population plus N_PERM_GENE
# per population for the per-gene null. Start SMALL (e.g. 50) to gauge runtime on
# the 1060, then raise. Set N_PERM_GENE=0 to skip per-gene p-values entirely.
N_PERM = 200             # population-level null draws (multi-gene random KO)
N_PERM_GENE = 50         # per-gene null draws (single random-gene KO); 0 disables
FDR_ALPHA = 0.05         # Benjamini-Hochberg significance level
PERM_SEED = 0            # base RNG seed for the nulls (stable across resume)

OUTDIR = "perturbo_out"; MAPDIR = f"{OUTDIR}/maps"
os.makedirs(MAPDIR, exist_ok=True)
device = "cuda" if torch.cuda.is_available() else "cpu"
_orig = normalize_g2g

# ---------------- validated machinery (copied from your working scripts) ----------------
def normalize_g2g_sparse_signed(g):
    g = (g + g.T) / 2; g[g < -1] = -1; g[g > 1] = 1
    absg = g.abs() if hasattr(g, "abs") else np.abs(g)
    g[absg < THRESHOLD] = 0
    for i in range(len(g)): g[i, i] = 1
    return g

def prep(path):
    a = sc.read_h5ad(path)
    if SAMPLE_ID not in a.obs: a.obs[SAMPLE_ID] = "0"
    sc.pp.normalize_total(a, target_sum=1e6); sc.pp.log1p(a)
    if not issparse(a.X): a.X = csr_matrix(a.X)
    tmp = tempfile.NamedTemporaryFile(suffix=".h5ad", delete=False).name; a.write(tmp)
    return tmp, a

def unit_norm(a):
    X = a.X.toarray() if hasattr(a.X, "toarray") else np.asarray(a.X)
    X = X.astype("float32"); nf = np.sqrt((X**2).sum(1, keepdims=True)); nf[nf == 0] = 1
    return X / nf

def knockout(expr, genes, rows):
    e = expr.copy(); e[np.ix_(rows, genes)] = 0.0
    nf = np.sqrt((e**2).sum(1, keepdims=True)); nf[nf == 0] = 1
    return e / nf

def edges_of(coords):
    g = kneighbors_graph(coords, K, include_self=False).toarray()
    return torch.from_numpy(np.array(np.where(g == 1))).long()

def init_sparse(n):
    m = np.random.RandomState(SEED).uniform(-1, 1, size=(n, n)).astype("float32")
    return torch.from_numpy(normalize_g2g_sparse_signed((m + m.T) / 2))

def train_sparse(loader, n):
    T.normalize_g2g = normalize_g2g_sparse_signed
    m = celcomen(input_dim=n, output_dim=n, n_neighbors=K, seed=SEED)
    m.set_g2g(init_sparse(n)); m.set_g2g_intra(init_sparse(n)); m.to(device)
    T.train(CC_EPOCHS, CC_LR, m, loader, zmft_scalar=ZMFT, seed=SEED, device=device)
    T.normalize_g2g = _orig
    return m

def relax(model, expr_pert, edges, n):
    scm = simcomen(input_dim=n, output_dim=n, n_neighbors=K, seed=SEED)
    scm.set_g2g(model.conv1.lin.weight.clone().detach())
    scm.set_g2g_intra(model.lin.weight.clone().detach()); scm.to(device)
    scm.set_sphex(torch.nan_to_num(calc_sphex(torch.from_numpy(expr_pert))).float().to(device))
    opt = torch.optim.SGD(scm.parameters(), lr=1e-3, momentum=0)
    for _ in range(SCM_STEPS):
        msg, mi, lz = scm(edges.to(device), 1)
        loss = -(-lz + ZMFT*torch.trace(torch.mm(msg, scm.gex.t()))
                 + ZMFT*torch.trace(torch.mm(mi, scm.gex.t())))
        loss.backward(); opt.step(); opt.zero_grad()
    return scm.gex.detach().cpu().numpy()

# ---------------- statistical evaluation (your permutation-null layer) ----------
def _bh_fdr(pvals):
    """Benjamini-Hochberg q-values. Same correction used elsewhere in the pipeline."""
    p = np.asarray(pvals, dtype=float); m = p.size
    if m == 0:
        return p
    order = np.argsort(p)
    ranked = p[order] * m / (np.arange(m) + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(m); out[order] = np.clip(q, 0.0, 1.0)
    return out

def _tumour_stat(cf, base, tum_rows, tum_cols):
    """Magnitude of the mean per-tumour-gene response -- the same quantity the old
    `specificity` ratio was built on, now used as the permutation test statistic."""
    d = (cf - base)[tum_rows][:, tum_cols].mean(0)
    return float(np.abs(d).mean())

def perm_null_population(model, expr, base, edges, n, src, k, tum_rows, tum_cols, exclude, rng, n_perm):
    """Null for a POPULATION KO: knock out k RANDOM genes (of the same size as the
    driver set, drawn from genes that are NOT the drivers) in the same spots, many
    times. Returns the null distribution of the tumour-response magnitude."""
    pool = np.array([j for j in range(n) if j not in exclude], dtype=int)
    if pool.size == 0:
        return np.zeros(0)
    stats = np.empty(n_perm, dtype=float)
    for t in range(n_perm):
        rnd = rng.choice(pool, size=min(k, pool.size), replace=False)
        cfr = relax(model, knockout(expr, list(rnd), src), edges, n)
        stats[t] = _tumour_stat(cfr, base, tum_rows, tum_cols)
    return stats

def perm_null_single(model, expr, base, edges, n, src, tum_rows, tum_cols, exclude, rng, n_perm):
    """Null for a SINGLE-gene KO: knock out one RANDOM non-driver gene in the same
    spots, many times. One null distribution serves every driver gene in the
    population (all are single-gene KOs of the same form). Returns SIGNED effects."""
    pool = np.array([j for j in range(n) if j not in exclude], dtype=int)
    if pool.size == 0 or n_perm <= 0:
        return np.zeros(0)
    stats = np.empty(n_perm, dtype=float)
    for t in range(n_perm):
        j = int(rng.choice(pool))
        cfg = relax(model, knockout(expr, [j], src), edges, n)
        stats[t] = float((cfg - base)[tum_rows][:, tum_cols].mean())
    return stats

def _emp_p(observed_abs, null_abs):
    """One-sided empirical p on magnitude: P(random >= observed), with +1 smoothing."""
    null_abs = np.asarray(null_abs, dtype=float)
    if null_abs.size == 0:
        return float("nan")
    return float((1 + int((null_abs >= observed_abs).sum())) / (1 + null_abs.size))

def network_profile(model, gene_names):
    """Centrality of each gene in the learned signed coupling network (the G2G).
    degree = number of strong couplings (|w|>=THRESHOLD, off-diagonal) -> hub-ness.
    pos/neg_degree = supportive vs antagonistic couplings. Returns dict gene -> profile.
    NOTE: 'hub' = central in the learned coupling network (causal-given-model), not a
    proven master regulator."""
    Wraw = model.conv1.lin.weight.detach().cpu().numpy()
    W = np.asarray(normalize_g2g_sparse_signed((Wraw + Wraw.T) / 2), dtype=float)
    n = W.shape[0]; np.fill_diagonal(W, 0)
    strong = np.abs(W) >= THRESHOLD
    deg = strong.sum(1)
    pos_deg = ((W >= THRESHOLD)).sum(1)
    neg_deg = ((W <= -THRESHOLD)).sum(1)
    # hub threshold: top ~20% by degree (relative, so it adapts to the panel)
    hub_cut = np.percentile(deg[deg > 0], 80) if (deg > 0).any() else 1
    prof = {}
    for i, g in enumerate(gene_names):
        prof[g] = {"degree": int(deg[i]),
                   "supportive_links": int(pos_deg[i]),
                   "antagonistic_links": int(neg_deg[i]),
                   "is_hub": bool(deg[i] >= hub_cut and deg[i] > 0)}
    return prof, float(hub_cut)

# ---------------- spatial map (dark styling, matches your figures) ----------------
def save_map(coords, tum, src_mask, before_full, after_full, path, pop, section):
    """before_full/after_full are FULL-length per-spot tumour scores (0 outside tumour spots).
    We mask to tumour spots here, once, so sizes always match coords/tum."""
    diff_full = after_full - before_full
    x, y = coords[:, 0], coords[:, 1]
    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(6, 6))
    fig.patch.set_facecolor("#0d0d0d"); ax.set_facecolor("#0d0d0d")
    ax.scatter(x, y, s=6, c="#141414")
    dtum = diff_full[tum]
    lim = np.percentile(np.abs(dtum), 98) + 1e-12
    s = ax.scatter(x[tum], y[tum], s=14, c=dtum, cmap="RdBu_r",
                   norm=TwoSlopeNorm(0, -lim, lim), edgecolors="#000", linewidths=0.15)
    ax.scatter(x[src_mask], y[src_mask], s=7, c="none", edgecolors="#E0A458", linewidths=0.4)
    ax.set_title(f"{section}: knock out {pop}\nblue = tumour suppressed", color="w", fontsize=11)
    ax.set_xticks([]); ax.set_yticks([]); ax.set_aspect("equal")
    fig.colorbar(s, ax=ax, fraction=0.045, label="tumour Δ")
    fig.savefig(path, facecolor="#0d0d0d", bbox_inches="tight", dpi=130); plt.close(fig)

# ---------------- the engine: one section -> dossiers ----------------
def run_section(name, h5, drvp, label):
    print(f"\n===== {name} ({label}) =====")
    tmp, a = prep(h5); gene_names = list(a.var_names); n = len(gene_names)
    g2i = {g: i for i, g in enumerate(gene_names)}
    drv = pd.read_csv(drvp)
    ct = a.obs[CELLTYPE_COL].astype(str).values
    themes = a.obs[THEME_COL].astype(str).values
    tum_rows = np.isin(themes, list(TUMOUR_THEMES))
    tum_cols = [g2i[g] for g in drv[drv.theme.isin(TUMOUR_THEMES)].gene.unique() if g in g2i]
    coords = a.obsm["spatial"]
    expr = unit_norm(a); edges = edges_of(coords)
    loader = get_dataset_loaders(tmp, sample_id_name=SAMPLE_ID, n_neighbors=K,
                                 distance=None, device=device, verbose=False)
    os.unlink(tmp)
    print("  training model..."); model = train_sparse(loader, n)
    print("  computing network centrality..."); prof, hub_cut = network_profile(model, gene_names)
    base = relax(model, expr, edges, n)
    n_spots = base.shape[0]
    base_tum_full = np.zeros(n_spots, dtype=float)                 # full-length per-spot tumour score
    base_tum_full[tum_rows] = base[tum_rows][:, tum_cols].mean(1)

    populations = []
    pop_counts = pd.Series(ct).value_counts()
    for pop, n_sp in pop_counts.items():
        sub = drv[drv.cell_type == pop].head(TOP) if "cell_type" in drv.columns else pd.DataFrame()
        gidx = [g2i[g] for g in sub.gene if g in g2i]
        src = np.where(ct == pop)[0]
        if not gidx or len(src) == 0:
            continue
        # population-level effect: KO all its drivers
        cf = relax(model, knockout(expr, gidx, src), edges, n)
        d_gene = (cf - base)[tum_rows][:, tum_cols].mean(0)      # per tumour-gene response
        pop_effect = float(d_gene.mean())
        # per-driver-gene effect: KO each gene alone (the gene-level target ranking)
        gene_effects = []
        for g in sub.gene:
            if g not in g2i: continue
            cfg = relax(model, knockout(expr, [g2i[g]], src), edges, n)
            eff = float((cfg - base)[tum_rows][:, tum_cols].mean())
            p = prof.get(g, {"degree": 0, "supportive_links": 0, "antagonistic_links": 0, "is_hub": False})
            gene_effects.append({
                "gene": g, "effect": round(eff, 6),
                "degree": p["degree"], "is_hub": p["is_hub"],
                "supportive_links": p["supportive_links"],
                "antagonistic_links": p["antagonistic_links"],
                "_eff_raw": eff,
                # p_value / q_value / priority_target are filled in after the null + FDR
            })

        # ---- statistical evaluation: permutation nulls (the evaluation layer) ----
        # Deterministic per-population seed so nulls are stable across resume.
        rng = np.random.RandomState(PERM_SEED + sum(ord(c) for c in str(pop)))
        exclude = set(gidx)                       # nulls draw from NON-driver genes
        pop_obs = float(np.abs(d_gene).mean())    # observed tumour-response magnitude
        pop_null = perm_null_population(model, expr, base, edges, n, src, len(gidx),
                                        tum_rows, tum_cols, exclude, rng, N_PERM)
        pop_p = _emp_p(pop_obs, pop_null)
        pop_z = float((pop_obs - pop_null.mean()) / (pop_null.std() + 1e-12)) if pop_null.size else float("nan")
        # specificity kept for backward-compat, now = observed / mean(null distribution)
        spec = float(pop_obs / (pop_null.mean() + 1e-9)) if pop_null.size else float("nan")
        # one single-gene null distribution serves every driver gene in this population
        gene_null = perm_null_single(model, expr, base, edges, n, src,
                                     tum_rows, tum_cols, exclude, rng, N_PERM_GENE)
        for rec in gene_effects:
            rec["p_value"] = (_emp_p(abs(rec["_eff_raw"]), np.abs(gene_null))
                              if gene_null.size else float("nan"))

        # spatial map
        after_tum_full = np.zeros(n_spots, dtype=float)
        after_tum_full[tum_rows] = cf[tum_rows][:, tum_cols].mean(1)
        mp = f"{MAPDIR}/{name}_{pop.replace(' ','_').replace('/','_')}.png"
        save_map(coords, tum_rows, (ct == pop), base_tum_full, after_tum_full, mp, pop, label)

        populations.append({
            "name": pop,
            "n_spots": int(n_sp),
            "theme": sub.theme.iloc[0] if len(sub) else "?",
            "tumour_effect": round(pop_effect, 6),
            "specificity": round(spec, 2) if spec == spec else None,
            "p_value": round(pop_p, 4) if pop_p == pop_p else None,
            "z_score": round(pop_z, 2) if pop_z == pop_z else None,
            "null_mean": round(float(pop_null.mean()), 6) if pop_null.size else None,
            "null_sd": round(float(pop_null.std()), 6) if pop_null.size else None,
            "n_perm": int(N_PERM),
            "powered": bool(n_sp >= 100),
            "_gene_effects": gene_effects,       # finalized after section-wide FDR
            "spatial_map": os.path.relpath(mp, OUTDIR),
        })
        flag = "" if n_sp >= 100 else " (underpowered)"
        pstr = f"p={pop_p:.3f}" if pop_p == pop_p else "p=NA"
        print(f"  {pop:26s} effect={pop_effect:+.5f} {pstr} spec={spec:.1f} n={n_sp}{flag}")

    # ---- section-wide Benjamini-Hochberg FDR: across populations, and across genes ----
    pop_ps = [p["p_value"] for p in populations if p["p_value"] is not None]
    if pop_ps:
        pop_q = _bh_fdr(pop_ps); qi = 0
        for p in populations:
            if p["p_value"] is not None:
                p["q_value"] = round(float(pop_q[qi]), 4); qi += 1
                p["significant"] = bool(p["q_value"] < FDR_ALPHA)
            else:
                p["q_value"] = None; p["significant"] = None
    else:
        for p in populations:
            p["q_value"] = None; p["significant"] = None

    all_recs = [rec for p in populations for rec in p["_gene_effects"]]
    gene_ps = [rec["p_value"] for rec in all_recs if rec.get("p_value") == rec.get("p_value") and rec.get("p_value") is not None]
    if gene_ps:
        gene_q = _bh_fdr(gene_ps); qi = 0
        for rec in all_recs:
            pv = rec.get("p_value")
            if pv is not None and pv == pv:
                rec["q_value"] = round(float(gene_q[qi]), 4); qi += 1
                rec["significant"] = bool(rec["q_value"] < FDR_ALPHA)
            else:
                rec["q_value"] = None; rec["significant"] = None
    else:
        for rec in all_recs:
            rec["q_value"] = None; rec["significant"] = None

    # priority target now REQUIRES the effect to beat the permutation null (FDR-significant),
    # not just be negative + hub. This is the rigor the raw specificity ratio lacked.
    for p in populations:
        for rec in p["_gene_effects"]:
            eff = rec.pop("_eff_raw")
            rec["p_value"] = round(float(rec["p_value"]), 4) if rec["p_value"] == rec["p_value"] else None
            rec["priority_target"] = bool(eff < 0 and rec["is_hub"] and bool(rec.get("significant")))
        p["_gene_effects"].sort(key=lambda r: (not r["priority_target"], r["effect"]))
        p["top_genes"] = p["_gene_effects"][:N_TOP_GENES]
        del p["_gene_effects"]

    # rank populations by most suppressive effect
    populations.sort(key=lambda p: p["tumour_effect"])
    for i, p in enumerate(populations): p["rank"] = i + 1
    return {"label": label, "n_spots_total": int(len(ct)),
            "n_tumour_readout_genes": len(tum_cols),
            "evaluation": {"n_perm_population": int(N_PERM), "n_perm_gene": int(N_PERM_GENE),
                           "fdr_alpha": FDR_ALPHA,
                           "note": ("priority_target requires the single-gene KO effect to be "
                                    "suppressive AND the gene to be a network hub AND the effect "
                                    "to beat a permutation null at FDR<alpha. 'significant' = FDR<alpha. "
                                    "p is one-sided (random KO matches/beats observed magnitude).")},
            "populations": populations}

if __name__ == "__main__":
    outpath = f"{OUTDIR}/perturbo_data.json"
    # resume: load existing results if present, skip sections already done
    if os.path.exists(outpath):
        with open(outpath) as f:
            data = json.load(f)
        print(f"found existing {outpath} with sections: {list(data.get('sections', {}).keys())}")
    else:
        data = {"tool": "Perturbo", "unit": "cell population -> target gene -> spatial",
                "disclaimer": "Predicted, causal-given-model. Effects are evaluated against a "
                              "permutation null (random-gene KO) with BH-FDR; priority targets are "
                              "FDR-significant, suppressive, network hubs. Still nominated hypotheses "
                              "requiring experimental validation.",
                "sections": {}}
    print(f"evaluation: population null={N_PERM} draws, per-gene null={N_PERM_GENE} draws, "
          f"FDR alpha={FDR_ALPHA}")
    print("  NOTE: each null draw is a full `relax`. If a section is slow, lower N_PERM / "
          "N_PERM_GENE (set N_PERM_GENE=0 to skip per-gene p-values). Test small first.")
    for nm, (h5, drvp, lab) in SECTIONS.items():
        if nm in data["sections"]:
            print(f"skipping {nm} (already computed and saved)")
            continue
        try:
            data["sections"][nm] = run_section(nm, h5, drvp, lab)
            # SAVE IMMEDIATELY after each section finishes (crash-proof)
            with open(outpath, "w") as f:
                json.dump(data, f, indent=2)
            print(f"  ✓ saved {nm} to {outpath}")
        except Exception as e:
            import traceback; print(f"  {nm} FAILED: {type(e).__name__}: {e}"); traceback.print_exc()
            print(f"  (other sections are still saved; rerun to resume {nm})")
    print(f"\n✓ done. {outpath} has sections: {list(data['sections'].keys())}")
    print(f"✓ spatial maps in {MAPDIR}/")
    print("\nThis JSON is Perturbo's brain — the app and agent will read it next.")
