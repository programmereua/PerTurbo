import sys, subprocess

def pipinstall(*pkgs):
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", *pkgs])

for module, package in [("causallearn", "causal-learn"), ("networkx", "networkx"),
                         ("statsmodels", "statsmodels"), ("omnipath", "omnipath"),
                         ("scipy", "scipy"), ("sklearn", "scikit-learn"),
                         ("matplotlib", "matplotlib"), ("pandas", "pandas"),
                         ("joblib", "joblib")]:
    try:
        __import__(module)
    except ImportError:
        pipinstall(package)

import os, glob, zipfile, json
import numpy as np

DATA_FOLDER = os.getcwd()

for z in glob.glob(os.path.join(DATA_FOLDER, "*.zip")):
    if "outputs_end2end" in os.path.basename(z).lower():
        target = os.path.join(DATA_FOLDER, "unzipped_outputs")
        if not os.path.isdir(target):
            with zipfile.ZipFile(z) as f:
                f.extractall(target)
        DATA_FOLDER = target

def find_file(name):
    hits = sorted(glob.glob(os.path.join(DATA_FOLDER, "**", name), recursive=True))
    return hits[0] if hits else None

OUT = os.path.join(os.getcwd(), "causal_results")
os.makedirs(OUT, exist_ok=True)

sections = {}
for name in ["HM11", "T11"]:
    expr_path = find_file(f"expr_{name}.npy")
    meta_path = find_file(f"sec_meta_{name}.json")
    w_path = find_file(f"W_{name}.npy")
    if not (expr_path and meta_path):
        continue
    meta = json.load(open(meta_path))
    sections[name] = dict(
        expr=np.load(expr_path), meta=meta,
        genes=meta["gene_names"], gene_index={g: i for i, g in enumerate(meta["gene_names"])},
        W=(np.load(w_path) if w_path else None))

if not sections:
    raise FileNotFoundError("No data found in " + DATA_FOLDER)

# gene panels
STROMA_GENES = ["THBS1", "TIMP1", "BGN", "TAGLN", "COL1A1", "ACTA2", "IGFBP7", "CCN2", "SERPINE1", "MMP2"]
TUMOUR_GENES = ["KRT8", "ITGA3", "GPRC5A", "S100A6", "EPCAM", "CLDN4", "KRT7", "MUC1", "LGALS4", "TSPAN8"]

def panel_for(name):
    present = sections[name]["gene_index"]
    stroma = [g for g in STROMA_GENES if g in present]
    tumour = [g for g in TUMOUR_GENES if g in present]
    return stroma, tumour

# effect size and FDR per stroma-tumour pair
from itertools import combinations
from scipy.stats import norm
from statsmodels.stats.multitest import multipletests
import pandas as pd

def partial_correlation_pvalue(X, i, j, conditioning=()):
    idx = [i, j] + list(conditioning)
    corr = np.corrcoef(X[:, idx].T)
    try:
        precision = np.linalg.inv(corr)
    except np.linalg.LinAlgError:
        return 1.0, 0.0
    r = -precision[0, 1] / np.sqrt(precision[0, 0] * precision[1, 1] + 1e-12)
    r = float(np.clip(r, -0.999, 0.999))
    n = len(X)
    z = 0.5 * np.log((1 + r) / (1 - r)) * np.sqrt(max(n - len(conditioning) - 3, 1))
    return 2 * (1 - norm.cdf(abs(z))), r

effect_tables = {}
for name in sections:
    stroma, tumour = panel_for(name)
    panel = stroma + tumour
    idx = [sections[name]["gene_index"][g] for g in panel]
    X = sections[name]["expr"][:, idx]
    role = {g: ("stroma" if g in stroma else "tumour") for g in panel}

    rows = []
    for a, b in combinations(range(len(panel)), 2):
        if role[panel[a]] == role[panel[b]]:
            continue
        p_value, partial_r = partial_correlation_pvalue(X, a, b)
        r = float(np.corrcoef(X[:, a], X[:, b])[0, 1])
        s_gene = panel[a] if role[panel[a]] == "stroma" else panel[b]
        t_gene = panel[b] if role[panel[b]] == "tumour" else panel[a]
        rows.append(dict(stroma=s_gene, tumour=t_gene,
                          correlation=round(r, 3), variance_explained=round(r * r, 3),
                          partial_r=round(partial_r, 3), p_raw=p_value))
    df = pd.DataFrame(rows)
    df["p_corrected"] = multipletests(df.p_raw, alpha=0.10, method="fdr_bh")[1]
    df["passes_fdr"] = df.p_corrected < 0.10
    df["meaningful_effect"] = df.correlation.abs() > 0.15
    effect_tables[name] = df.sort_values("variance_explained", ascending=False)

# bootstrap FCI
from causallearn.search.ConstraintBased.FCI import fci
from causallearn.utils.cit import fisherz
from causallearn.graph.Endpoint import Endpoint
from collections import defaultdict
from joblib import Parallel, delayed
import time

def edge_kind(edge):
    if edge is None:
        return None
    a, b = edge.get_endpoint1(), edge.get_endpoint2()
    if a == Endpoint.ARROW and b == Endpoint.ARROW:
        return "latent"
    if Endpoint.CIRCLE in (a, b):
        return "uncertain"
    return "directed"

def edge_direction(edge, names, i, j):
    if edge is None:
        return None
    a, b = edge.get_endpoint1(), edge.get_endpoint2()
    if a == Endpoint.TAIL and b == Endpoint.ARROW:
        return (names[i], names[j])
    if a == Endpoint.ARROW and b == Endpoint.TAIL:
        return (names[j], names[i])
    return None

FCI_DEPTH = 3
N_BOOTSTRAP = 35
SUBSAMPLE = 2000
N_JOBS = -1

def _one_bootstrap_fci(X, seed, depth, subsample):
    rng = np.random.RandomState(seed)
    m = min(len(X), subsample)
    resample = X[rng.randint(0, len(X), m)]
    try:
        graph, _ = fci(resample, fisherz, alpha=0.05, depth=depth, verbose=False, show_progress=False)
        return graph
    except Exception:
        return None

bootstrap_results = {}
for name in sections:
    stroma, tumour = panel_for(name)
    panel = stroma + tumour
    idx = [sections[name]["gene_index"][g] for g in panel]
    X = sections[name]["expr"][:, idx]
    role = {g: ("stroma" if g in stroma else "tumour") for g in panel}

    kind_counts = defaultdict(lambda: defaultdict(int))
    direction_counts = defaultdict(lambda: defaultdict(int))

    graphs = Parallel(n_jobs=N_JOBS, backend="loky", prefer="processes")(
        delayed(_one_bootstrap_fci)(X, seed, FCI_DEPTH, SUBSAMPLE) for seed in range(N_BOOTSTRAP)
    )

    for graph in graphs:
        if graph is None:
            continue
        nodes = graph.get_nodes()
        for i in range(len(panel)):
            for j in range(i + 1, len(panel)):
                edge = graph.get_edge(nodes[i], nodes[j])
                kind = edge_kind(edge)
                pair = (panel[i], panel[j])
                if kind:
                    kind_counts[pair][kind] += 1
                    kind_counts[pair]["present"] += 1
                direction = edge_direction(edge, panel, i, j)
                if direction:
                    direction_counts[pair][direction] += 1

    bootstrap_results[name] = dict(kinds=kind_counts, directions=direction_counts, role=role)

# nonparametric confirmation on the strongest pairs
from causallearn.utils.cit import CIT

def kci_check(name, pairs):
    stroma, tumour = panel_for(name)
    panel = stroma + tumour
    idx = [sections[name]["gene_index"][g] for g in panel]
    X = sections[name]["expr"][:, idx]
    position = {g: k for k, g in enumerate(panel)}
    rng = np.random.RandomState(0)
    sample = rng.choice(len(X), min(600, len(X)), replace=False)
    test = CIT(X[sample], "kci")
    results = {}
    for a, b in pairs:
        if a in position and b in position:
            try:
                results[(a, b)] = float(test(position[a], position[b], []))
            except Exception:
                results[(a, b)] = np.nan
    return results

kci_results = {}
for name in sections:
    counts = bootstrap_results[name]["kinds"]
    role = bootstrap_results[name]["role"]
    shortlist = [pair for pair, v in sorted(counts.items(), key=lambda kv: -kv[1].get("present", 0))
                 if role[pair[0]] != role[pair[1]] and v.get("present", 0) / N_BOOTSTRAP > 0.6][:8]
    kci_results[name] = kci_check(name, shortlist)

# literature priors from OmniPath
import omnipath

interactions = omnipath.interactions.OmniPath().get(genesymbols=True)
interactions = interactions[interactions["consensus_direction"] == True]
wanted = {g.upper() for g in STROMA_GENES + TUMOUR_GENES}
omni_edges = {}
for _, row in interactions.iterrows():
    a = str(row["source_genesymbol"]).upper()
    b = str(row["target_genesymbol"]).upper()
    if a in wanted and b in wanted and a != b:
        sign = 1 if row.get("consensus_stimulation") else (-1 if row.get("consensus_inhibition") else 0)
        omni_edges[(a, b)] = sign
omnipath_edges = {name: dict(omni_edges) for name in sections}

def literature_support(stroma_gene, tumour_gene, name):
    a, b = stroma_gene.upper(), tumour_gene.upper()
    in_omnipath = (a, b) in omnipath_edges[name] or (b, a) in omnipath_edges[name]
    sign = omnipath_edges[name].get((a, b), omnipath_edges[name].get((b, a), 0))
    return in_omnipath, sign

# decision rule
def decide(pair, name):
    stroma_gene, tumour_gene = pair
    boot = bootstrap_results[name]
    kinds = boot["kinds"].get((stroma_gene, tumour_gene)) or boot["kinds"].get((tumour_gene, stroma_gene), {})
    present = kinds.get("present", 0) / N_BOOTSTRAP
    directed = kinds.get("directed", 0) / N_BOOTSTRAP
    latent = kinds.get("latent", 0) / N_BOOTSTRAP

    directions = boot["directions"].get((stroma_gene, tumour_gene), {})
    best_direction, best_fraction = None, 0.0
    for (src, dst), c in directions.items():
        if c / N_BOOTSTRAP > best_fraction:
            best_direction, best_fraction = f"{src} -> {dst}", c / N_BOOTSTRAP

    table = effect_tables[name].set_index(["stroma", "tumour"])
    try:
        row = table.loc[(stroma_gene, tumour_gene)]
        r = float(row["correlation"]); passes_fdr = bool(row["passes_fdr"]); strong = abs(r) > 0.15
    except KeyError:
        r, passes_fdr, strong = float("nan"), False, False

    has_literature, sign = literature_support(stroma_gene, tumour_gene, name)

    axes = sum([present > 0.8, passes_fdr, strong, has_literature])

    if present > 0.8 and passes_fdr and strong and has_literature:
        verdict = "HIGH"
    elif axes >= 2:
        verdict = "MEDIUM"
    else:
        verdict = "LOW"

    if latent > max(directed, 0.5):
        call = "shared latent factor (the niche), not a direct arrow"
    elif best_direction and best_fraction > 0.3:
        call = best_direction
    else:
        call = "direction undetermined"

    reasons = [
        f"stable in {present:.0%} of bootstraps",
        "passes FDR" if passes_fdr else "does not pass FDR",
        f"effect size |r|={abs(r):.2f}" + (" (meaningful)" if strong else " (weak)"),
        "literature-backed" if has_literature else "no literature support",
    ]
    return dict(section=name, stroma=stroma_gene, tumour=tumour_gene,
                verdict=verdict, call=call, correlation=r,
                present=round(present, 2), directed=round(directed, 2), latent=round(latent, 2),
                passes_fdr=passes_fdr, in_omnipath=has_literature,
                axes_supported=axes, reasons="; ".join(reasons))

final_tables = {}
for name in sections:
    role = bootstrap_results[name]["role"]
    pairs = [pair for pair in bootstrap_results[name]["kinds"] if role[pair[0]] != role[pair[1]]]
    decisions = [decide(pair, name) for pair in pairs]
    df = pd.DataFrame(decisions).sort_values(["verdict", "axes_supported", "present"], ascending=[True, False, False])
    final_tables[name] = df

if len(final_tables) == 2:
    a, b = list(final_tables)
    calls_a = {(r.stroma, r.tumour): r.call for r in final_tables[a].itertuples()}
    calls_b = {(r.stroma, r.tumour): r.call for r in final_tables[b].itertuples()}
    for name in final_tables:
        final_tables[name]["repeats_in_both"] = [
            (r.stroma, r.tumour) in calls_a and (r.stroma, r.tumour) in calls_b
            and calls_a[(r.stroma, r.tumour)] == calls_b[(r.stroma, r.tumour)]
            and "undetermined" not in str(calls_a[(r.stroma, r.tumour)])
            for r in final_tables[name].itertuples()]

for name, df in final_tables.items():
    df.to_csv(os.path.join(OUT, f"causal_directions_{name}.csv"), index=False)
    high = sum(df.verdict == "HIGH"); med = sum(df.verdict == "MEDIUM"); low = sum(df.verdict == "LOW")
    print(f"{name}: {high} high, {med} medium, {low} low")
    print(df[["stroma", "tumour", "verdict", "call", "correlation", "reasons"]].head(10).to_string(index=False))

for name in sections:
    print(f"\n{name}: top calls")
    for row in final_tables[name].head(5).itertuples():
        print(f"{row.stroma} -> {row.tumour} | {row.verdict} | {row.call} | {row.reasons}")

# figures
import matplotlib.pyplot as plt
import networkx as nx
from matplotlib.lines import Line2D

def draw_section(name, df):
    graph = nx.DiGraph(); latent_links = []; node_role = {}
    for row in df.itertuples():
        node_role[row.stroma] = "stroma"; node_role[row.tumour] = "tumour"
        if row.verdict == "LOW":
            continue
        if "latent" in str(row.call):
            latent_links.append((row.stroma, row.tumour, row.present))
        elif "->" in str(row.call):
            src, dst = [x.strip() for x in row.call.split("->")]
            graph.add_edge(src, dst, weight=row.present, verdict=row.verdict, literature=row.in_omnipath)
    all_nodes = set(node_role) | set(graph.nodes()) | {x for e in latent_links for x in e[:2]}
    layout_graph = nx.Graph()
    layout_graph.add_nodes_from(all_nodes)
    layout_graph.add_edges_from(list(graph.edges()) + [(a, b) for a, b, _ in latent_links])
    if len(layout_graph) == 0:
        return
    pos = nx.spring_layout(layout_graph, seed=7, k=3.0 / np.sqrt(len(layout_graph)), iterations=250)

    fig, (ax_graph, ax_heat) = plt.subplots(1, 2, figsize=(20, 11), gridspec_kw={"width_ratios": [1.4, 1]})
    colours = {"stroma": "#E0A458", "tumour": "#B07BC7"}
    nx.draw_networkx_nodes(layout_graph, pos,
        node_color=[colours.get(node_role.get(g, "?"), "#888") for g in layout_graph.nodes()],
        node_size=[900 + 260 * layout_graph.degree(g) for g in layout_graph.nodes()],
        edgecolors="#111", linewidths=1.6, ax=ax_graph)
    nx.draw_networkx_labels(layout_graph, pos, font_size=11, font_weight="bold", ax=ax_graph)
    verdict_colour = {"HIGH": "#2a8a4a", "MEDIUM": "#c9922e"}
    for u, v, d in graph.edges(data=True):
        ax_graph.annotate("", xy=pos[v], xytext=pos[u], arrowprops=dict(
            arrowstyle="-|>", color=verdict_colour.get(d["verdict"], "#999"),
            lw=2 + 4.5 * d["weight"], ls="-" if d["literature"] else "--",
            alpha=0.9, connectionstyle="arc3,rad=0.09", shrinkA=16, shrinkB=16))
    for a, b, w in latent_links:
        ax_graph.annotate("", xy=pos[b], xytext=pos[a], arrowprops=dict(
            arrowstyle="<|-|>", color="#b23b3b", lw=1.8 + 3.5 * w, ls=":",
            alpha=0.85, connectionstyle="arc3,rad=0.16", shrinkA=16, shrinkB=16))
    ax_graph.legend(handles=[
        Line2D([0], [0], color="#2a8a4a", lw=4, label="high confidence direction"),
        Line2D([0], [0], color="#c9922e", lw=4, label="medium confidence direction"),
        Line2D([0], [0], color="#b23b3b", lw=3, ls=":", label="shared latent factor"),
        Line2D([0], [0], color="#555", lw=3, ls="--", label="not in the literature"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#E0A458", markersize=13, label="stroma gene"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#B07BC7", markersize=13, label="tumour gene"),
    ], loc="upper left", fontsize=10, frameon=True)
    ax_graph.set_title(f"{name}: stroma to tumour directions", fontsize=14)
    ax_graph.axis("off")

    stroma, tumour = panel_for(name)
    strength = np.full((len(stroma), len(tumour)), np.nan)
    lookup = {(r.stroma, r.tumour): r for r in df.itertuples()}
    for i, a in enumerate(stroma):
        for j, b in enumerate(tumour):
            if (a, b) in lookup:
                strength[i, j] = abs(lookup[(a, b)].correlation)
    im = ax_heat.imshow(strength, cmap="magma", vmin=0, vmax=0.3, aspect="auto")
    ax_heat.set_xticks(range(len(tumour))); ax_heat.set_xticklabels(tumour, rotation=60, ha="right", fontsize=9)
    ax_heat.set_yticks(range(len(stroma))); ax_heat.set_yticklabels(stroma, fontsize=9)
    ax_heat.set_title(f"{name}: effect size", fontsize=13)
    plt.colorbar(im, ax=ax_heat, fraction=0.046, label="strength of correlation")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, f"causal_graph_{name}.png"), dpi=300, bbox_inches="tight")
    fig.savefig(os.path.join(OUT, f"causal_graph_{name}.pdf"), bbox_inches="tight")
    plt.close(fig)

for name, df in final_tables.items():
    draw_section(name, df)
