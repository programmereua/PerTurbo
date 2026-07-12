"""
gene_knowledge.py  ---  grounded gene knowledge lookup (real databases, no hallucination)
Every field traces to a real API record: Open Targets, Europe PMC, ClinicalTrials.gov, DGIdb.
Used by the Perturbo agent as the lookup_gene_knowledge tool.
"""
import json, urllib.request, urllib.parse, concurrent.futures as cf

# gene synonyms — query external DBs under BOTH names and merge (old names carry many drug/trial records)
ALIASES = {
    "CCN1": ["CYR61"], "CCN2": ["CTGF"], "CCN3": ["NOV"], "CCN4": ["WISP1"],
    "CCN5": ["WISP2"], "CCN6": ["WISP3"],
    "CXCL8": ["IL8"], "SPP1": ["OPN"], "SERPINE1": ["PAI1"],
}
def _aliases(sym):
    s = sym.upper()
    out = [s] + ALIASES.get(s, [])
    for k, v in ALIASES.items():
        if s in v and k not in out: out.append(k)
    return out

_TIMEOUT = 12
def _get(url):
    try:
        r = urllib.request.urlopen(url, timeout=_TIMEOUT)
        return json.loads(r.read())
    except Exception:
        return None
def _post(url, body):
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=_TIMEOUT).read())
    except Exception:
        return None

def _ensembl_id(sym):
    """resolve a gene symbol -> Ensembl gene id via Open Targets search."""
    q = {"query": "query($q:String!){ search(queryString:$q, entityNames:[\"target\"]){ hits{ id name } } }",
         "variables": {"q": sym}}
    d = _post("https://api.platform.opentargets.org/api/v4/graphql", q)
    try:
        for h in d["data"]["search"]["hits"]:
            if h["id"].startswith("ENSG"): return h["id"]
    except Exception: pass
    return None

def _opentargets(sym):
    eid = _ensembl_id(sym)
    if not eid: return {"available": False}
    # current OT schema: knownDrugs was removed from Target; tractability has modality/value only.
    q = {"query": """query($id:String!){ target(ensemblId:$id){
            approvedSymbol approvedName biotype
            tractability{ modality value }
         } }""", "variables": {"id": eid}}
    d = _post("https://api.platform.opentargets.org/api/v4/graphql", q)
    try:
        t = d["data"]["target"]
        tract = sorted({r["modality"] for r in (t.get("tractability") or []) if r.get("value")})
        return {"available": True, "ensembl_id": eid, "name": t.get("approvedName"),
                "biotype": t.get("biotype"), "tractability": tract}
    except Exception:
        return {"available": False}

def _dgidb(sym_list):
    """DGIdb GraphQL (v2 REST is dead). Accepts a list of aliases; merges drug names."""
    names = json.dumps([s.upper() for s in sym_list])
    q = {"query": "{ genes(names: %s) { nodes { name interactions { drug { name } "
                  "interactionTypes { type } } } } }" % names}
    d = _post("https://dgidb.org/api/graphql", q)
    try:
        drugs = {}; total = 0
        for node in d["data"]["genes"]["nodes"]:
            for it in (node.get("interactions") or []):
                nm = (it.get("drug") or {}).get("name")
                if not nm: continue
                total += 1
                typ = ";".join(sorted({t.get("type") for t in (it.get("interactionTypes") or []) if t.get("type")}))
                drugs.setdefault(nm, typ)
        drug_list = [{"drug": k, "type": v} for k, v in list(drugs.items())[:14]]
        return {"available": True, "n_drug_interactions": total,
                "drugs": [d["drug"] for d in drug_list],
                "drug_detail": drug_list}
    except Exception:
        return {"available": False}

def _europepmc(syms, disease="pancreatic cancer"):
    # OR across aliases, e.g. (CCN2 OR CTGF) AND "pancreatic cancer"
    gene_or = " OR ".join(syms)
    query = f'({gene_or}) AND {disease}'
    q = urllib.parse.quote(query)
    d = _get(f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={q}&format=json&pageSize=3&sort=CITED%20desc")
    try:
        res = d["resultList"]["result"]; total = d.get("hitCount", 0)
        papers = [{"title": r.get("title"), "year": r.get("pubYear"),
                   "journal": r.get("journalTitle"), "id": r.get("id"),
                   "cited": r.get("citedByCount")} for r in res[:3]]
        return {"available": True, "total_hits": total, "top_papers": papers, "query": query}
    except Exception:
        return {"available": False}

def _trials(syms, disease="pancreatic cancer"):
    # ClinicalTrials.gov: query each alias, merge unique NCTs
    seen = {}; total = 0
    for sym in syms:
        q = urllib.parse.quote(f"{sym} {disease}")
        d = _get(f"https://clinicaltrials.gov/api/v2/studies?query.term={q}&pageSize=5&countTotal=true")
        if not d: continue
        total = max(total, d.get("totalCount", 0))
        for s in d.get("studies", []):
            p = s.get("protocolSection", {})
            nct = p.get("identificationModule", {}).get("nctId")
            if nct and nct not in seen:
                seen[nct] = {"nct": nct,
                             "title": p.get("identificationModule", {}).get("briefTitle"),
                             "phase": (p.get("designModule", {}).get("phases") or ["?"])[0],
                             "status": p.get("statusModule", {}).get("overallStatus")}
    trials = list(seen.values())
    return {"available": True, "total_trials": max(total, len(trials)), "trials": trials[:4]}

def lookup_gene_knowledge(gene, disease="pancreatic cancer"):
    """Grounded multi-DB dossier for one gene. Queries under all known aliases and merges.
    Runs the 4 sources in parallel. Every field is a real database record."""
    sym = str(gene).strip().upper()
    syms = _aliases(sym)                     # e.g. CCN2 -> [CCN2, CTGF]
    out = {"gene": sym, "aliases_queried": syms, "disease_context": disease, "sources": {}}
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_opentargets, sym): "open_targets",     # OT resolves the modern symbol
                ex.submit(_europepmc, syms, disease): "literature",
                ex.submit(_trials, syms, disease): "clinical_trials",
                ex.submit(_dgidb, syms): "drug_interactions"}
        for f in cf.as_completed(futs):
            out["sources"][futs[f]] = f.result() or {"available": False}
    ot = out["sources"].get("open_targets", {})
    lit = out["sources"].get("literature", {})
    tr = out["sources"].get("clinical_trials", {})
    dg = out["sources"].get("drug_interactions", {})
    bits = []
    if ot.get("available") and ot.get("tractability"):
        bits.append("druggable: " + ", ".join(ot["tractability"][:3]))
    if dg.get("available") and dg.get("n_drug_interactions"):
        bits.append(f"{dg['n_drug_interactions']} drug interactions (DGIdb)")
    if lit.get("available"):
        bits.append(f"{lit.get('total_hits',0)} papers (Europe PMC)")
    if tr.get("available") and tr.get("total_trials"):
        bits.append(f"{tr['total_trials']} clinical trials")
    alias_note = f" (queried as {'/'.join(syms)})" if len(syms) > 1 else ""
    out["summary"] = ("; ".join(bits) + alias_note) if bits else f"limited public records for {sym} in {disease}."
    out["disclaimer"] = "Sourced live from public databases (Open Targets, Europe PMC, ClinicalTrials.gov, DGIdb); presence of a drug/trial does not imply efficacy for this target."
    return out

if __name__ == "__main__":
    import sys
    g = sys.argv[1] if len(sys.argv) > 1 else "CCN2"
    print(json.dumps(lookup_gene_knowledge(g), indent=2))
