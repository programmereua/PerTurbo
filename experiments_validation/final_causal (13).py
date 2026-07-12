import os
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
# These must be set BEFORE numpy is imported anywhere in this process (and
# they are inherited by every worker process spawned later), otherwise
# OpenBLAS/MKL will size their internal thread pools to the full core count
# and N worker processes will oversubscribe the machine by ~N times.

def main():
    import sys, subprocess

    def pipinstall(*pkgs):
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", *pkgs])

    print("[1/10] checking dependencies")
    for module, package in [("causallearn", "causal-learn"), ("networkx", "networkx"),
                             ("statsmodels", "statsmodels"), ("omnipath", "omnipath"),
                             ("scipy", "scipy"), ("sklearn", "scikit-learn"),
                             ("matplotlib", "matplotlib"), ("pandas", "pandas"),
                             ("joblib", "joblib")]:
        try:
            __import__(module)
        except ImportError:
            print(f"    installing {package} ...")
            pipinstall(package)
    print("    dependencies ready")

    import os, glob, zipfile, json
    import numpy as np

    print("[2/10] locating data")
    DATA_FOLDER = os.getcwd()

    for z in glob.glob(os.path.join(DATA_FOLDER, "*.zip")):
        if "outputs_end2end" in os.path.basename(z).lower():
            target = os.path.join(DATA_FOLDER, "unzipped_outputs")
            if not os.path.isdir(target):
                print(f"    unzipping {os.path.basename(z)}")
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
    print(f"    loaded sections: {list(sections)}")

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

    print("[3/10] computing effect sizes and FDR correction")
    effect_tables = {}
    for name in sections:
        print(f"    {name}: scanning stroma-tumour pairs")
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
        print(f"    {name}: {len(df)} pairs tested, {int(df.passes_fdr.sum())} pass FDR")

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

    # ---------------------------------------------------------------------
    # Two independent conditional-independence tests, both bootstrapped.
    #
    # Fisher-z is a LINEAR test. Benchmarked on this data it disagrees with a
    # kernel test on 33% of stroma-tumour pairs, and in every single
    # disagreement Fisher-z declares independence while the kernel test finds
    # real dependence. That directly inflates FCI's latent-confounder calls,
    # because a pair FCI wrongly believes is independent cannot be given a
    # direct edge. So a fisherz-only graph is not trustworthy here.
    #
    # The fix is RCIT: a kernel test approximated with random Fourier features,
    # so it captures the nonlinearity Fisher-z misses without KCI's O(n^2)
    # blow-up. Measured on this panel (12 nodes):
    #     fisherz  n=800 depth=2 ->   0.6 s
    #     RCIT     n=400 depth=2 ->   9.4 s   (feasible to bootstrap)
    #     KCI      n=400 depth=1 -> 112.0 s   (NOT feasible to bootstrap)
    # So we bootstrap BOTH fisherz and RCIT and compare them. Runtime stays in
    # the same order of magnitude as before, and every verdict now carries an
    # explicit linear-vs-nonlinear agreement flag.
    # ---------------------------------------------------------------------
    FCI_DEPTH = 1   # was 2. On this 20-node panel, depth=2 makes each FCI run
                    # 66-300s+ (some exceed any sane timeout and are LOST, which
                    # is what hurts reliability). depth=1 conditions on at most one
                    # variable instead of two: every run finishes in seconds-to-
                    # minutes, so ALL bootstraps complete and the fractions are
                    # clean. Trade-off: depth=1 is a less aggressive skeleton search
                    # and can leave in a few adjacencies (and thus orientations)
                    # that depth=2 would remove, so treat it as the reliable
                    # overnight pass. To rerun the fuller version later, set this
                    # back to 2 AND delete causal_results/*.pkl first.
    N_BOOTSTRAP = 15
    SUBSAMPLE = 800          # rows per fisherz FCI call
    SUBSAMPLE_RCIT = 400     # rows per RCIT FCI call (tuned: 9.4s/run)
    N_JOBS = -1
    RUN_ALPHA_SENSITIVITY = True
    ALT_ALPHA = 0.10

    CI_TESTS = ["fisherz", "rcit"]        # both are bootstrapped, then compared
    PRIMARY_CI = "rcit"                   # the nonlinear test is the one we trust
    SUBSAMPLE_FOR = {"fisherz": SUBSAMPLE, "rcit": SUBSAMPLE_RCIT}
    BOOTSTRAP_TIMEOUT = 1800               # seconds; a single bootstrap run that exceeds
                                             # this is treated as failed (same as an exception),
                                             # so one pathological resample cannot stall the run.
                                             # At depth=1 (20 nodes) runs finish in seconds-to-
                                             # minutes, so 1800s is a generous backstop that will
                                             # rarely fire. Budget math: 60 bootstrap runs on ~7
                                             # workers, even if every run hit this cap, is
                                             # 60*1800/7 ~= 4.3h worst case -- comfortably < 8h.

    def _one_bootstrap_fci(X, seed, depth, subsample, ci_test):
        import threading
        t_start = time.time()
        print(f"        [{ci_test}] bootstrap {seed+1}/{N_BOOTSTRAP} starting")
        rng = np.random.RandomState(seed)
        m = min(len(X), subsample)
        resample = X[rng.randint(0, len(X), m)]
        # de-duplicate rows: bootstrap-with-replacement can draw the same spot
        # many times, and duplicate rows can make a kernel CI test's internal
        # matrix near-singular, which is the most likely cause of an occasional
        # pathologically slow run. Working on unique rows removes that risk and
        # does not change what FCI is testing (repeated identical observations
        # carry no extra information for a correlation/kernel-based CI test).
        resample = np.unique(resample, axis=0)
        if len(resample) < 20:
            print(f"        [{ci_test}] bootstrap {seed+1}/{N_BOOTSTRAP} skipped: too few unique rows after dedup")
            return None

        stop = threading.Event()
        def _heartbeat():
            tick = 0
            while not stop.wait(8):
                tick += 8
                print(f"        [{ci_test}] bootstrap {seed+1}/{N_BOOTSTRAP} still running, {tick}s elapsed")
        hb = threading.Thread(target=_heartbeat, daemon=True)
        hb.start()
        try:
            from threadpoolctl import threadpool_limits
            with threadpool_limits(limits=1):
                graph, _ = fci(resample, ci_test, alpha=0.05, depth=depth, verbose=False, show_progress=False)
            stop.set()
            print(f"        [{ci_test}] bootstrap {seed+1}/{N_BOOTSTRAP} finished in {time.time()-t_start:.1f}s")
            return graph
        except Exception as e:
            stop.set()
            print(f"        [{ci_test}] bootstrap {seed+1}/{N_BOOTSTRAP} failed after {time.time()-t_start:.1f}s: {e}")
            return None

    # ONE persistent worker pool, created once and reused for every bootstrap
    # call across every section and every CI test. Creating a fresh pool per
    # task was measured to cost ~100x more than reusing one (0.95s vs 0.01s
    # for trivial tasks in a lightweight test, and dramatically more on a real
    # machine where every fresh process must reimport numpy/scipy/causal-learn
    # from scratch) -- that overhead, not the actual FCI computation, is what
    # was causing every single bootstrap call to time out at 120s.
    from joblib.externals.loky import get_reusable_executor
    import concurrent.futures as _cf

    def _available_cpus():
        # os.cpu_count() reports the HOST node's core count, which on Colab /
        # Codespaces / any cgroup-limited container is far larger than the number
        # of cores THIS process is actually allowed to use. Sizing the pool from
        # it spawns e.g. 15-31 FCI worker processes onto 2 physical cores, and the
        # resulting oversubscription is what turns a 0.6s Fisher-z run into minutes
        # (and lets timed-out runs pile up and starve everything else).
        # sched_getaffinity respects the cgroup/affinity mask, so it returns the
        # real usable count; fall back to cpu_count only where it is unavailable.
        try:
            return len(os.sched_getaffinity(0))
        except AttributeError:            # non-Linux (e.g. Windows)
            return os.cpu_count() or 2

    _USABLE = _available_cpus()
    _N_POOL_WORKERS = max(1, _USABLE - 1)  # leave one core free for the main process
    _EXECUTOR = get_reusable_executor(max_workers=_N_POOL_WORKERS)
    print(f"    detected {_USABLE} usable CPU(s) (affinity-aware); "
          f"persistent worker pool ready with {_N_POOL_WORKERS} workers "
          f"(created once, reused for all bootstraps)")

    def _one_bootstrap_fci_bounded(X, seed, depth, subsample, ci_test, timeout):
        """Submits one bootstrap FCI call to the persistent pool with a hard
        wall-clock timeout. If it times out, that single result is treated as
        a failed run (same as an exception) -- the worker that was running it
        may stay busy in the background until it naturally finishes, but this
        does not block or delay the other already-submitted tasks, since they
        run on the other workers in the same pool."""
        try:
            fut = _EXECUTOR.submit(_one_bootstrap_fci, X, seed, depth, subsample, ci_test)
            return fut.result(timeout=timeout)
        except _cf.TimeoutError:
            print(f"        [{ci_test}] bootstrap {seed+1}/{N_BOOTSTRAP} TIMED OUT after {timeout}s, "
                  f"treated as a failed run (this does not affect the other bootstrap runs)")
            return None
        except Exception as e:
            print(f"        [{ci_test}] bootstrap {seed+1}/{N_BOOTSTRAP} failed: {e}")
            return None

    print("[4/10] running bootstrap FCI under BOTH a linear and a nonlinear CI test")
    import pickle

    def _plain_nested(d):
        """Convert a defaultdict(lambda: defaultdict(int)) into plain dict-of-dict.
        Standard pickle cannot serialize a lambda (it has no importable name), so
        anything built with defaultdict(lambda: ...) must be converted to a plain
        dict before it is written to disk. This is what crashed the previous run
        after 35 minutes: the lambda-based defaultdict was pickled directly."""
        return {k: dict(v) for k, v in d.items()}

    def _aggregate(graphs, panel):
        kind_counts = defaultdict(lambda: defaultdict(int))
        direction_counts = defaultdict(lambda: defaultdict(int))
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
        # return PLAIN dicts -- picklable regardless of how they were built
        return _plain_nested(kind_counts), _plain_nested(direction_counts)

    # bootstrap_all[ci_test][section] -> dict(kinds=..., directions=..., role=...)
    bootstrap_all = {ci: {} for ci in CI_TESTS}
    for ci_test in CI_TESTS:
        print(f"\n  === CI test: {ci_test} ===")
        for name in sections:
            checkpoint = os.path.join(OUT, f"bootstrap_{ci_test}_{name}.pkl")
            if os.path.exists(checkpoint):
                with open(checkpoint, "rb") as f:
                    bootstrap_all[ci_test][name] = pickle.load(f)
                print(f"    {name}: loaded cached {ci_test} bootstrap from {checkpoint}")
                continue
            stroma, tumour = panel_for(name)
            panel = stroma + tumour
            idx = [sections[name]["gene_index"][g] for g in panel]
            X = sections[name]["expr"][:, idx]
            role = {g: ("stroma" if g in stroma else "tumour") for g in panel}
            sub_n = SUBSAMPLE_FOR[ci_test]

            t0 = time.time()
            print(f"    {name}: submitting {N_BOOTSTRAP} {ci_test} FCI runs to the persistent pool "
                  f"(n={sub_n}, depth={FCI_DEPTH}, per-run timeout={BOOTSTRAP_TIMEOUT}s)")
            # submit all N_BOOTSTRAP tasks to the SAME persistent pool at once;
            # the pool's _N_POOL_WORKERS workers pick them up and run them
            # concurrently, with no per-task pool creation overhead.
            futs = [_EXECUTOR.submit(_one_bootstrap_fci, X, seed, FCI_DEPTH, sub_n, ci_test)
                    for seed in range(N_BOOTSTRAP)]
            graphs = []
            for seed, fut in enumerate(futs):
                try:
                    graphs.append(fut.result(timeout=BOOTSTRAP_TIMEOUT))
                except _cf.TimeoutError:
                    print(f"        [{ci_test}] bootstrap {seed+1}/{N_BOOTSTRAP} TIMED OUT after "
                          f"{BOOTSTRAP_TIMEOUT}s, treated as a failed run")
                    graphs.append(None)
                except Exception as e:
                    print(f"        [{ci_test}] bootstrap {seed+1}/{N_BOOTSTRAP} failed: {e}")
                    graphs.append(None)
            ok = sum(g is not None for g in graphs)
            kind_counts, direction_counts = _aggregate(graphs, panel)
            print(f"    {name}: {ok}/{N_BOOTSTRAP} {ci_test} runs finished in {time.time()-t0:.0f}s")

            bootstrap_all[ci_test][name] = dict(kinds=kind_counts, directions=direction_counts, role=role)
            with open(checkpoint, "wb") as f:
                pickle.dump(bootstrap_all[ci_test][name], f)
            print(f"    {name}: saved checkpoint to {checkpoint}")

    # the nonlinear test is the estimator we trust; fisherz is kept for comparison
    bootstrap_results = bootstrap_all[PRIMARY_CI]

    # ---------------------------------------------------------------------
    # Is the latent-confounder rate real, or an artefact of the linear test?
    # ---------------------------------------------------------------------
    print("\n  === latent-confounder rate: linear vs nonlinear CI test ===")
    latent_rate_report = {}
    for name in sections:
        rates = {}
        for ci_test in CI_TESTS:
            b = bootstrap_all[ci_test][name]
            role = b["role"]
            cross = [p for p in b["kinds"] if role[p[0]] != role[p[1]]]
            if not cross:
                continue
            n_latent = sum(1 for p in cross
                           if b["kinds"][p].get("latent", 0) > max(b["kinds"][p].get("directed", 0),
                                                                    0.5 * N_BOOTSTRAP))
            n_directed = sum(1 for p in cross
                             if b["kinds"][p].get("directed", 0) > b["kinds"][p].get("latent", 0))
            rates[ci_test] = dict(latent=n_latent, directed=n_directed, total=len(cross))
        latent_rate_report[name] = rates
        print(f"    {name}:")
        for ci_test, r in rates.items():
            print(f"        {ci_test:8s}: {r['latent']:2d}/{r['total']} latent ({r['latent']/max(r['total'],1):.0%}), "
                  f"{r['directed']:2d}/{r['total']} directed ({r['directed']/max(r['total'],1):.0%})")
        if "fisherz" in rates and "rcit" in rates:
            fz_l = rates["fisherz"]["latent"] / max(rates["fisherz"]["total"], 1)
            rc_l = rates["rcit"]["latent"] / max(rates["rcit"]["total"], 1)
            if fz_l - rc_l > 0.10:
                print(f"        -> the linear test inflated the latent rate by {100*(fz_l-rc_l):.0f} points; "
                      f"the nonlinear (RCIT) figure is the one to report")
            elif rc_l - fz_l > 0.10:
                print(f"        -> the nonlinear test finds MORE latent confounding, not less")
            else:
                print(f"        -> both tests broadly agree, so the latent rate is not a linear-test artefact")

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

    print("[5/10] checking sensitivity to the FCI significance threshold")
    alpha_agreement = {}
    if RUN_ALPHA_SENSITIVITY:
        for name in sections:
            sens_checkpoint = os.path.join(OUT, f"alpha_sensitivity_{name}.pkl")
            if os.path.exists(sens_checkpoint):
                with open(sens_checkpoint, "rb") as f:
                    alpha_agreement[name] = pickle.load(f)
                print(f"    {name}: loaded cached alpha sensitivity from {sens_checkpoint}")
                continue
            stroma, tumour = panel_for(name)
            panel = stroma + tumour
            idx = [sections[name]["gene_index"][g] for g in panel]
            X = sections[name]["expr"][:, idx]
            rng = np.random.RandomState(0)
            X_fixed = X[rng.randint(0, len(X), min(len(X), SUBSAMPLE_FOR[PRIMARY_CI]))]

            print(f"    {name}: running FCI once at alpha=0.05 (base)")
            t0 = time.time()
            from threadpoolctl import threadpool_limits
            with threadpool_limits(limits=1):
                base_graph, _ = fci(X_fixed, PRIMARY_CI, alpha=0.05, depth=FCI_DEPTH, verbose=False, show_progress=False)
            print(f"    {name}: base run finished in {time.time()-t0:.0f}s")
            base_nodes = base_graph.get_nodes()
            base_kind = {}
            for i in range(len(panel)):
                for j in range(i + 1, len(panel)):
                    base_kind[(panel[i], panel[j])] = edge_kind(base_graph.get_edge(base_nodes[i], base_nodes[j]))

            print(f"    {name}: running FCI once at alpha={ALT_ALPHA} (alternate)")
            t0 = time.time()
            with threadpool_limits(limits=1):
                alt_graph, _ = fci(X_fixed, PRIMARY_CI, alpha=ALT_ALPHA, depth=FCI_DEPTH, verbose=False, show_progress=False)
            print(f"    {name}: alternate run finished in {time.time()-t0:.0f}s")
            alt_nodes = alt_graph.get_nodes()

            agree = {}
            for i in range(len(panel)):
                for j in range(i + 1, len(panel)):
                    pair = (panel[i], panel[j])
                    alt_kind = edge_kind(alt_graph.get_edge(alt_nodes[i], alt_nodes[j]))
                    agree[pair] = (alt_kind == base_kind[pair])
            alpha_agreement[name] = agree
            with open(sens_checkpoint, "wb") as f:
                pickle.dump(agree, f)
            stable = sum(agree.values())
            print(f"    {name}: {stable}/{len(agree)} pairs give the same edge type at both thresholds")
    else:
        print("    skipped (RUN_ALPHA_SENSITIVITY is False)")

    print("[6/10] running the nonparametric KCI check on the strongest pairs")
    kci_results = {}
    for name in sections:
        counts = bootstrap_results[name]["kinds"]
        role = bootstrap_results[name]["role"]
        shortlist = [pair for pair, v in sorted(counts.items(), key=lambda kv: -kv[1].get("present", 0))
                     if role[pair[0]] != role[pair[1]] and v.get("present", 0) / N_BOOTSTRAP > 0.6][:8]
        print(f"    {name}: KCI on {len(shortlist)} shortlisted pairs")
        t0 = time.time()
        kci_results[name] = kci_check(name, shortlist)
        print(f"    {name}: KCI finished in {time.time()-t0:.0f}s")

    # literature priors from OmniPath
    print("[7/10] fetching literature priors from OmniPath and KEGG")
    wanted = {g.upper() for g in STROMA_GENES + TUMOUR_GENES}

    # Gene-symbol aliases. Some databases (KEGG in particular) still list a gene
    # under its legacy symbol first -- e.g. CCN2 is stored as "CTGF, CCN2, ..." so
    # an exact first-token match silently drops it. We canonicalise every alias
    # back to the panel symbol so no directed edge is lost to a name change.
    GENE_ALIASES = {
        "CCN2":     {"CCN2", "CTGF"},
        "CCN1":     {"CCN1", "CYR61"},
        "SERPINE1": {"SERPINE1", "PAI1", "PAI-1", "PLANH1"},
        "TAGLN":    {"TAGLN", "SM22", "SM22A"},
        "THBS1":    {"THBS1", "TSP1", "TSP-1"},
    }
    alias_to_canon = {g: g for g in wanted}
    for canon, al in GENE_ALIASES.items():
        if canon in wanted:
            for a in al:
                alias_to_canon[a.upper()] = canon
    def _canon(sym):
        return alias_to_canon.get(str(sym).upper(), str(sym).upper())
    def _aliases_of(g):
        return {a.upper() for a in GENE_ALIASES.get(g, {g})} | {g}

    print("    querying OmniPath")
    omni_edges = {}
    try:
        import omnipath
        interactions = omnipath.interactions.OmniPath().get(genesymbols=True)
        interactions = interactions[interactions["consensus_direction"] == True]
        for _, row in interactions.iterrows():
            a = _canon(row["source_genesymbol"])
            b = _canon(row["target_genesymbol"])
            if a in wanted and b in wanted and a != b:
                sign = 1 if row.get("consensus_stimulation") else (-1 if row.get("consensus_inhibition") else 0)
                omni_edges[(a, b)] = sign
        print(f"    OmniPath: {len(omni_edges)} directed edges among the panel genes")
    except Exception as e:
        print(f"    OmniPath unreachable, continuing without it: {e}")

    print("    querying KEGG")
    kegg_edges = {}
    try:
        import requests, re
        gene_to_kegg_id = {}
        for g in wanted:
            r = requests.get(f"https://rest.kegg.jp/find/hsa/{g}", timeout=10)
            r.raise_for_status()
            for line in r.text.strip().split("\n"):
                if not line:
                    continue
                kid, desc = line.split("\t", 1)
                # KEGG lists all symbols for the gene before the ';', e.g.
                # "CTGF, CCN2, HCS24, ...". Match g against ALL of them (and its
                # known aliases), not just the first, so legacy-first entries like
                # CCN2 (listed as CTGF first) are no longer silently dropped.
                symbols = {s.strip().upper() for s in desc.split(";")[0].split(",")}
                if symbols & _aliases_of(g):
                    gene_to_kegg_id[g] = kid.split(":")[1]
                    break

        pathway_ids = set()
        for g, kid in gene_to_kegg_id.items():
            r = requests.get(f"https://rest.kegg.jp/link/pathway/hsa:{kid}", timeout=10)
            r.raise_for_status()
            for line in r.text.strip().split("\n"):
                if not line:
                    continue
                _, pw = line.split("\t", 1)
                pathway_ids.add(pw)

        id_to_gene = {v: k for k, v in gene_to_kegg_id.items()}
        for pw in pathway_ids:
            r = requests.get(f"https://rest.kegg.jp/get/{pw}/kgml", timeout=15)
            r.raise_for_status()
            xml = r.text
            entry_gene = {}
            for m in re.finditer(r'<entry id="(\d+)"[^>]*name="hsa:(\d+)"', xml):
                gene = id_to_gene.get(m.group(2))
                if gene:
                    entry_gene[m.group(1)] = gene
            for m in re.finditer(r'<relation entry1="(\d+)" entry2="(\d+)"[^>]*type="\w+">(.*?)</relation>', xml, re.S):
                e1, e2, body = m.groups()
                g1, g2 = entry_gene.get(e1), entry_gene.get(e2)
                if not g1 or not g2 or g1 == g2:
                    continue
                sign = 1 if "activation" in body else (-1 if "inhibition" in body else 0)
                kegg_edges[(g1, g2)] = sign
        print(f"    KEGG: {len(kegg_edges)} directed edges among the panel genes, from {len(pathway_ids)} pathways")
    except Exception as e:
        print(f"    KEGG unreachable, continuing without it: {e}")

    omnipath_edges = {name: dict(omni_edges) for name in sections}
    kegg_edges_by_section = {name: dict(kegg_edges) for name in sections}

    def literature_support(stroma_gene, tumour_gene, name):
        a, b = stroma_gene.upper(), tumour_gene.upper()
        omni = omnipath_edges[name]; kegg = kegg_edges_by_section[name]
        in_omni = (a, b) in omni or (b, a) in omni
        in_kegg = (a, b) in kegg or (b, a) in kegg
        omni_sign = omni.get((a, b), omni.get((b, a), 0))
        kegg_sign = kegg.get((a, b), kegg.get((b, a), 0))
        sign = omni_sign if omni_sign != 0 else kegg_sign
        source = "OmniPath+KEGG" if (in_omni and in_kegg) else ("OmniPath" if in_omni else ("KEGG" if in_kegg else "none"))
        # Which way does the literature point? The edge stores are keyed by an
        # ordered (source, target) pair, so the key that matches gives us the
        # literature direction in the panel's own gene names.
        lit_direction = None
        for store in (omni, kegg):
            if (a, b) in store:
                lit_direction = f"{stroma_gene} -> {tumour_gene}"; break
            if (b, a) in store:
                lit_direction = f"{tumour_gene} -> {stroma_gene}"; break
        return (in_omni or in_kegg), sign, source, lit_direction

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

        has_literature, sign, lit_source, lit_direction = literature_support(stroma_gene, tumour_gene, name)

        threshold_stable = alpha_agreement.get(name, {}).get(pair)
        if threshold_stable is None:
            threshold_stable = alpha_agreement.get(name, {}).get((tumour_gene, stroma_gene))

        # Do the linear (fisherz) and nonlinear (RCIT) bootstraps reach the same
        # verdict for this pair? A pair whose call flips between the two tests is
        # driven by the choice of independence test, not by the data, and must not
        # be presented as a confident finding.
        def _dominant(bres):
            k = bres["kinds"].get((stroma_gene, tumour_gene)) or bres["kinds"].get((tumour_gene, stroma_gene), {})
            if not k:
                return "absent"
            lat = k.get("latent", 0); dirn = k.get("directed", 0); unc = k.get("uncertain", 0)
            return max((("latent", lat), ("directed", dirn), ("uncertain", unc)), key=lambda t: t[1])[0]

        ci_calls = {ci: _dominant(bootstrap_all[ci][name]) for ci in CI_TESTS if name in bootstrap_all[ci]}
        ci_tests_agree = (len(set(ci_calls.values())) == 1) if len(ci_calls) > 1 else None

        axes = sum([present > 0.8, passes_fdr, strong, has_literature])

        if (present > 0.8 and passes_fdr and strong and has_literature
                and threshold_stable is not False and ci_tests_agree is not False):
            verdict = "HIGH"
        elif axes >= 2 and ci_tests_agree is not False:
            verdict = "MEDIUM"
        else:
            verdict = "LOW"

        # Direction. Data-derived wherever the bootstraps orient the edge; only
        # when the data leaves it undetermined do we fall back to the literature,
        # and then it is labelled as a prior, NOT presented as a finding from
        # these Visium data.
        direction_source = "data"
        if latent > max(directed, 0.5):
            call = "shared latent factor (the niche), not a direct arrow"
        elif best_direction and best_fraction > 0.3:
            call = best_direction
        elif lit_direction is not None:
            call = lit_direction
            direction_source = "literature"
        else:
            call = "direction undetermined"

        if ci_tests_agree is True:
            ci_note = f"linear and nonlinear CI tests agree ({ci_calls.get(PRIMARY_CI, '?')})"
        elif ci_tests_agree is False:
            ci_note = ("linear and nonlinear CI tests DISAGREE ("
                       + ", ".join(f"{k}={v}" for k, v in ci_calls.items())
                       + ") - the call depends on the test, treat as unreliable")
        else:
            ci_note = "only one CI test available"

        reasons = [
            f"stable in {present:.0%} of bootstraps",
            "passes FDR" if passes_fdr else "does not pass FDR",
            f"effect size |r|={abs(r):.2f}" + (" (meaningful)" if strong else " (weak)"),
            f"literature: {lit_source}" if has_literature else "no literature support",
            ("same at alpha 0.05 and " + str(ALT_ALPHA) if threshold_stable else "changes with the significance threshold")
            if threshold_stable is not None else "threshold sensitivity not checked",
            ci_note,
        ]
        if direction_source == "literature":
            reasons.append(f"direction taken from literature ({lit_source}) as a prior - "
                           f"the data alone leave the direction undetermined")
        return dict(section=name, stroma=stroma_gene, tumour=tumour_gene,
                    verdict=verdict, call=call, correlation=r,
                    present=round(present, 2), directed=round(directed, 2), latent=round(latent, 2),
                    passes_fdr=passes_fdr, in_literature=has_literature, literature_source=lit_source,
                    direction_source=direction_source,
                    threshold_stable=threshold_stable,
                    ci_tests_agree=ci_tests_agree,
                    call_fisherz=ci_calls.get("fisherz"), call_rcit=ci_calls.get("rcit"),
                    axes_supported=axes, reasons="; ".join(reasons))

    print("[8/10] applying the decision rule to every pair")
    final_tables = {}
    for name in sections:
        role = bootstrap_results[name]["role"]
        pairs = [pair for pair in bootstrap_results[name]["kinds"] if role[pair[0]] != role[pair[1]]]
        decisions = [decide(pair, name) for pair in pairs]
        df = pd.DataFrame(decisions).sort_values(["verdict", "axes_supported", "present"], ascending=[True, False, False])
        final_tables[name] = df
        print(f"    {name}: {len(df)} pairs decided")

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

    print("[9/10] writing results")
    for name, df in final_tables.items():
        df.to_csv(os.path.join(OUT, f"causal_directions_{name}.csv"), index=False)
        high = sum(df.verdict == "HIGH"); med = sum(df.verdict == "MEDIUM"); low = sum(df.verdict == "LOW")
        print(f"    {name}: {high} high, {med} medium, {low} low -> saved causal_directions_{name}.csv")
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
        graph = nx.DiGraph(); latent_links = []; lit_edges = []; node_role = {}
        for row in df.itertuples():
            node_role[row.stroma] = "stroma"; node_role[row.tumour] = "tumour"
            ds = getattr(row, "direction_source", "data")
            # Literature-derived directions are priors, not findings from these
            # data, so we draw them in their own style regardless of verdict
            # (their reliability does not come from the bootstrap at all).
            if ds == "literature" and "->" in str(row.call):
                src, dst = [x.strip() for x in row.call.split("->")]
                lit_edges.append((src, dst, row.present))
                continue
            if row.verdict == "LOW":
                continue
            if "latent" in str(row.call):
                latent_links.append((row.stroma, row.tumour, row.present))
            elif "->" in str(row.call):
                src, dst = [x.strip() for x in row.call.split("->")]
                graph.add_edge(src, dst, weight=row.present, verdict=row.verdict, literature=row.in_literature,
                               threshold_stable=row.threshold_stable, ci_agree=row.ci_tests_agree)
        all_nodes = (set(node_role) | set(graph.nodes())
                     | {x for e in latent_links for x in e[:2]}
                     | {x for e in lit_edges for x in e[:2]})
        layout_graph = nx.Graph()
        layout_graph.add_nodes_from(all_nodes)
        layout_graph.add_edges_from(list(graph.edges())
                                    + [(a, b) for a, b, _ in latent_links]
                                    + [(a, b) for a, b, _ in lit_edges])
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
            faint = (d.get("threshold_stable") is False) or (d.get("ci_agree") is False)
            ax_graph.annotate("", xy=pos[v], xytext=pos[u], arrowprops=dict(
                arrowstyle="-|>", color=verdict_colour.get(d["verdict"], "#999"),
                lw=2 + 4.5 * d["weight"], ls="-" if d["literature"] else "--",
                alpha=0.45 if faint else 0.9, connectionstyle="arc3,rad=0.09", shrinkA=16, shrinkB=16))
        for a, b, w in latent_links:
            ax_graph.annotate("", xy=pos[b], xytext=pos[a], arrowprops=dict(
                arrowstyle="<|-|>", color="#b23b3b", lw=1.8 + 3.5 * w, ls=":",
                alpha=0.85, connectionstyle="arc3,rad=0.16", shrinkA=16, shrinkB=16))
        for a, b, w in lit_edges:
            ax_graph.annotate("", xy=pos[b], xytext=pos[a], arrowprops=dict(
                arrowstyle="-|>", color="#3b6ea5", lw=2.6, ls=(0, (5, 2)),
                alpha=0.9, connectionstyle="arc3,rad=0.09", shrinkA=16, shrinkB=16))
        ax_graph.legend(handles=[
            Line2D([0], [0], color="#2a8a4a", lw=4, label="high confidence direction"),
            Line2D([0], [0], color="#c9922e", lw=4, label="medium confidence direction"),
            Line2D([0], [0], color="#555", lw=3, alpha=0.45, label="faint: test- or threshold-dependent (unreliable)"),
            Line2D([0], [0], color="#b23b3b", lw=3, ls=":", label="shared latent factor"),
            Line2D([0], [0], color="#555", lw=3, ls="--", label="not in the literature"),
            Line2D([0], [0], color="#3b6ea5", lw=3, ls=(0, (5, 2)), label="direction from literature (prior, not from these data)"),
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

    print("[10/10] drawing figures")
    for name, df in final_tables.items():
        draw_section(name, df)
        print(f"    {name}: saved causal_graph_{name}.png and .pdf")
    print("done")



if __name__ == "__main__":
    main()
