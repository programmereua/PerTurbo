#!/usr/bin/env python3
"""
perturbo_network_export.py  ---  export REAL networks to network.json for the dashboard
========================================================================================
Faithful port of pathway_directed_network.py's method, emitting JSON (not a PNG) so the
dashboard renders it interactively (Cytoscape-style).

Per section:
  * UNDIRECTED signed Celcomen coupling network (G2G symmetric -> couplings are undirected LINES)
  * DIRECTED signed pathway priors overlaid (direction = literature, ARROWS)
  * SIGN CONCORDANCE: where a signed prior overlaps a coupling, does the data recover the sign?

Get W two ways:
  (A) --from-model : train sparse-signed Celcomen here (needs celcomen env + data)
  (B) --g2g/--genes: load a dumped signed G2G .npy + gene list (like the original script)

  python perturbo_network_export.py --from-model
  python perturbo_network_export.py --g2g g2g_HM11_signed.npy --genes genes_HM11.txt --section HM11
"""
import argparse, json, os, sys, tempfile, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")

CURATED_PRIORS = [
    ("TGFB1","TGFBR1",+1),("TGFB1","TGFBR2",+1),("TGFB1","SMAD2",+1),("TGFB1","SMAD3",+1),
    ("TGFB1","CCN2",+1),("TGFB1","SERPINE1",+1),("TGFB1","CCN1",+1),
    ("TGFB1","COL1A1",+1),("TGFB1","COL1A2",+1),("TGFB1","COL3A1",+1),("TGFB1","COL11A1",+1),
    ("TGFB1","COL5A1",+1),("TGFB1","COL5A2",+1),("TGFB1","COL6A1",+1),("TGFB1","COL6A3",+1),
    ("TGFB1","FN1",+1),("TGFB1","ACTA2",+1),("TGFB1","TIMP1",+1),("TGFB1","VIM",+1),
    ("TGFB1","SPARC",+1),("TGFB1","BGN",+1),("TGFB1","TAGLN",+1),("TGFB1","POSTN",+1),
    ("TGFB1","LOX",+1),("TGFB1","LOXL2",+1),("TGFB1","SERPINH1",+1),("TGFB1","FBN1",+1),
    ("TGFB1","THBS1",+1),("TGFB1","THBS2",+1),("TGFB1","DCN",+1),("TGFB1","LUM",+1),
    ("CCN2","ITGB1",+1),("CCN1","ITGB1",+1),("CCN2","COL1A1",+1),("CCN2","FN1",+1),("CCN1","FN1",+1),
    ("ACTA2","CNN1",+1),("ACTA2","TAGLN",+1),("ACTA2","CALD1",+1),("ACTA2","ACTG2",+1),
    ("ACTA2","MYH11",+1),("ACTA2","MYL9",+1),
    ("LOX","COL1A1",+1),("LOX","COL3A1",+1),("LOXL2","COL1A1",+1),("SERPINH1","COL1A1",+1),
    ("SERPINH1","COL3A1",+1),("SPARC","COL1A1",+1),("SPARC","COL3A1",+1),("BGN","COL1A1",+1),
    ("BGN","COL6A1",+1),("DCN","COL1A1",+1),("POSTN","COL1A1",+1),("POSTN","FN1",+1),
    ("COL1A1","ITGB1",+1),("COL1A2","ITGB1",+1),("FN1","ITGB1",+1),("FN1","ITGA5",+1),("FN1","COL1A1",+1),
    ("THBS1","CD47",+1),("THBS2","COL1A1",+1),("CXCL12","CXCR4",+1),("PDGFB","PDGFRB",+1),
    ("PDGFRB","ACTA2",+1),("PDGFRB","COL1A1",+1),("VIM","ACTA2",+1),("LUM","COL1A1",+1),
    ("FBN1","COL1A1",+1),("TIMP1","COL1A1",+1),
]
ALIASES = {"CYR61":"CCN1","CTGF":"CCN2","NOV":"CCN3","WISP1":"CCN4","WISP2":"CCN5","WISP3":"CCN6",
           "FISP12":"CCN2","IGFBP8":"CCN3"}
def _canon(g): return ALIASES.get(str(g).strip().upper(), str(g).strip().upper())

def load_priors(panel_genes):
    """MERGE all available sources (additive, not first-wins). Curated+OmniPath+KEGG carry SIGN (+/-);
    liana carries DIRECTION only (sign 0). Dedupe by (src,tgt); a signed edge overrides an unsigned one."""
    panel = set(_canon(g) for g in panel_genes)
    merged = {}   # (s,t) -> sign, with signed (+/-1) preferred over unsigned (0)
    used = []
    def add(edges, name):
        got = 0
        for s, t, sg in edges:
            if s in panel and t in panel and s != t:
                key = (s, t)
                if key not in merged or (merged[key] == 0 and sg != 0):
                    merged[key] = sg; got += 1
        if got: used.append(f"{name}({got})")
        return got

    # 1) curated signed (always available, citable)
    add([(_canon(s), _canon(t), sg) for (s, t, sg) in CURATED_PRIORS], "curated")

    # 2) OmniPath signed directed (needs network)
    try:
        import omnipath as op
        df = op.interactions.OmniPath().get()
        df = df[(df["is_directed"] == True) & ((df["is_stimulation"]==True) | (df["is_inhibition"]==True))]
        oe = [(_canon(r.get("source_genesymbol")), _canon(r.get("target_genesymbol")),
               1 if r["is_stimulation"] else -1) for _, r in df.iterrows()]
        add(oe, "OmniPath")
    except Exception as e:
        print(f"  priors: OmniPath unavailable ({type(e).__name__})")

    # 3) KEGG signed relations (needs network; optional)
    try:
        import requests, re as _re
        # small set of CAF/ECM-relevant KEGG pathways; parse activation/inhibition relations
        kegg_ids = ["hsa04350","hsa04510","hsa04512","hsa04151"]  # TGFb, focal adhesion, ECM-receptor, PI3K-Akt
        ke = []
        for pid in kegg_ids:
            try:
                kgml = requests.get(f"https://rest.kegg.jp/get/{pid}/kgml", timeout=8).text
                names = dict(_re.findall(r'<entry id="(\d+)"[^>]*name="([^"]+)"', kgml))
                for m in _re.finditer(r'<relation entry1="(\d+)" entry2="(\d+)"[^>]*>(.*?)</relation>', kgml, _re.S):
                    e1, e2, body = m.groups()
                    sg = 1 if "activation" in body else (-1 if "inhibition" in body else 0)
                    if sg == 0: continue
                    for g1 in names.get(e1, "").split():
                        for g2 in names.get(e2, "").split():
                            a1 = _canon(g1.replace("hsa:", "")); a2 = _canon(g2.replace("hsa:", ""))
                            # KEGG uses entrez ids in name; skip if not gene symbols -> handled by panel filter
                            ke.append((a1, a2, sg))
            except Exception: pass
        add(ke, "KEGG")
    except Exception as e:
        print(f"  priors: KEGG unavailable ({type(e).__name__})")

    # 4) liana consensus L-R (offline, direction only, unsigned)
    try:
        import liana, re as _re
        res = liana.resource.select_resource("consensus")
        le = []
        for lig, rec in zip(res["ligand"], res["receptor"]):
            ligs = [_canon(x) for x in _re.split(r"[_:&]", str(lig)) if x and x != "COMPLEX"]
            recs = [_canon(x) for x in _re.split(r"[_:&]", str(rec)) if x and x != "COMPLEX"]
            for s in ligs:
                for t in recs: le.append((s, t, 0))
        add(le, "liana")
    except Exception as e:
        print(f"  priors: liana unavailable ({type(e).__name__})")

    edges = [(s, t, sg) for (s, t), sg in merged.items()]
    src = "+".join(used) if used else "none"
    n_signed = sum(1 for _, _, sg in edges if sg != 0)
    print(f"  priors merged: {len(edges)} edges ({n_signed} signed) from {src}")
    return edges, src

def build_json(W, genes, niche, w_thresh, top_nodes):
    cg=[_canon(g) for g in genes]; idx={g:i for i,g in enumerate(cg)}; n=len(cg)
    asym=float(np.linalg.norm(W-W.T)/(np.linalg.norm(W)+1e-12))
    Wd=W.copy(); np.fill_diagonal(Wd,0)
    deg=(np.abs(Wd)>=w_thresh).sum(1)
    hub_cut=np.percentile(deg[deg>0],80) if (deg>0).any() else 1
    priors,src=load_priors(cg)
    und=[]
    for i in range(n):
        for j in range(i+1,n):
            if abs(Wd[i,j])>w_thresh:
                und.append((abs(Wd[i,j]),{"source":cg[i],"target":cg[j],
                    "weight":round(float(Wd[i,j]),3),"sign":"pos" if Wd[i,j]>0 else "neg"}))
    und.sort(key=lambda x:-x[0]); und_edges=[e for _,e in und[:120]]
    overlap=concord=signed_overlap=0; directed=[]; dir_nodes=set()
    for s,t,ps in priors:
        if s in idx and t in idx:
            w=Wd[idx[s],idx[t]]; has=abs(w)>w_thresh; dir_nodes.update([s,t])
            if has:
                overlap+=1
                if ps!=0:
                    signed_overlap+=1
                    if np.sign(w)==ps: concord+=1
            directed.append({"source":s,"target":t,"prior_sign":int(ps),
                "coupling_w":round(float(w),3),"overlaps":bool(has),
                "sign":("pos" if ps>0 else ("neg" if ps<0 else "unsigned"))})
    keep=set(int(i) for i in np.argsort(-deg)[:top_nodes] if deg[i]>0)
    for e in und_edges: keep.update([idx[e["source"]],idx[e["target"]]])
    for g in dir_nodes: keep.add(idx[g])
    keep=sorted(keep)
    nodes=[{"id":cg[i],"degree":int(deg[i]),"is_hub":bool(deg[i]>=hub_cut),
            "pos_links":int((Wd[i]>=w_thresh).sum()),"neg_links":int((Wd[i]<=-w_thresh).sum()),
            "is_upstream":any(d["source"]==cg[i] and d["overlaps"] for d in directed),
            "theme":str(niche.get(cg[i],niche.get(genes[i],"other")))} for i in keep]
    ks=set(cg[i] for i in keep)
    und_edges=[e for e in und_edges if e["source"] in ks and e["target"] in ks]
    directed=[d for d in directed if d["source"] in ks and d["target"] in ks]
    rep=dict(asymmetry=asym, symmetric=bool(asym<1e-3), prior_source=src,
             n_undirected=len(und_edges), n_directed=len(directed),
             overlap=overlap, signed_overlap=signed_overlap, concordant=concord,
             concordance=(concord/signed_overlap if signed_overlap else None))
    return {"nodes":nodes,"undirected_edges":und_edges,"directed_edges":directed,"report":rep}

def train_and_dump(section, h5, drvp):
    import scanpy as sc, torch
    from scipy.sparse import csr_matrix, issparse
    from celcomen.models.celcomen import celcomen
    from celcomen.datareaders.datareader import get_dataset_loaders
    from celcomen.utils.helpers import normalize_g2g
    import celcomen.training_plan.train as T
    SEED,K,CC_EPOCHS,CC_LR,ZMFT,THR=0,6,200,1e-1,1e-1,0.15
    def nrm(g):
        # works for BOTH torch tensors (called inside T.train) and numpy arrays
        g = (g + g.T) / 2
        g[g < -1] = -1; g[g > 1] = 1
        absg = g.abs() if hasattr(g, "abs") else np.abs(g)
        g[absg < THR] = 0
        for i in range(len(g)): g[i, i] = 1
        return g
    a=sc.read_h5ad(h5)
    if "sample_id" not in a.obs: a.obs["sample_id"]="0"
    sc.pp.normalize_total(a,target_sum=1e6); sc.pp.log1p(a)
    if not issparse(a.X): a.X=csr_matrix(a.X)
    genes=list(a.var_names); n=len(genes)
    tmp=tempfile.NamedTemporaryFile(suffix=".h5ad",delete=False).name; a.write(tmp)
    loader=get_dataset_loaders(tmp,sample_id_name="sample_id",n_neighbors=K,distance=None,device="cpu",verbose=False)
    os.unlink(tmp)
    m=np.random.RandomState(SEED).uniform(-1,1,(n,n)).astype("float32")
    T.normalize_g2g=nrm
    model=celcomen(input_dim=n,output_dim=n,n_neighbors=K,seed=SEED)
    model.set_g2g(torch.from_numpy(nrm((m+m.T)/2))); model.set_g2g_intra(torch.from_numpy(nrm((m+m.T)/2)))
    T.train(CC_EPOCHS,CC_LR,model,loader,zmft_scalar=ZMFT,seed=SEED,device="cpu")
    T.normalize_g2g=normalize_g2g
    W=model.conv1.lin.weight.detach().cpu().numpy(); W=np.asarray(nrm((W+W.T)/2),dtype=float)
    niche={}
    if drvp and os.path.exists(drvp):
        d=pd.read_csv(drvp)
        if "gene" in d.columns and "theme" in d.columns: niche=dict(zip(d.gene,d.theme))
    return W,genes,niche

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--from-model",action="store_true")
    ap.add_argument("--g2g"); ap.add_argument("--genes"); ap.add_argument("--niche"); ap.add_argument("--section",default="HM11")
    ap.add_argument("--w_thresh",type=float,default=0.05)
    ap.add_argument("--top_nodes",type=int,default=45)
    ap.add_argument("--out",default="network.json")
    a=ap.parse_args()
    out={"tool":"Perturbo","kind":"coupling_networks",
         "note":"Undirected lines = learned signed Celcomen couplings (data-derived, symmetric). "
                "Directed arrows = literature pathway direction (imported). Sign concordance = "
                "fraction of signed prior edges whose data-driven coupling agrees in sign.",
         "sections":{}}
    if a.from_model:
        SECTIONS={"HM11":("handoff_to_eva/data/IU_PDA_HM11.h5ad","handoff_to_eva/drivers/IU_PDA_HM11_drivers_per_celltype.csv","liver metastasis"),
                  "T11":("handoff_to_eva/data/IU_PDA_T11.h5ad","handoff_to_eva/drivers/IU_PDA_T11_drivers_per_celltype.csv","primary tumour")}
        for nm,(h5,drvp,lab) in SECTIONS.items():
            if not os.path.exists(h5): print(f"skip {nm}: {h5} not found"); continue
            print(f"\n== {nm} =="); W,genes,niche=train_and_dump(nm,h5,drvp)
            d=build_json(W,genes,niche,a.w_thresh,a.top_nodes); d["label"]=lab; out["sections"][nm]=d
            r=d["report"]; print(f"  {len(d['nodes'])} nodes, {r['n_undirected']} und, {r['n_directed']} dir"
                +(f", concordance {r['concordant']}/{r['signed_overlap']}" if r['signed_overlap'] else ""))
    else:
        if not (a.g2g and a.genes): sys.exit("provide --from-model OR --g2g and --genes")
        W=np.load(a.g2g)
        genes=[l.strip().split(",")[0] for l in open(a.genes) if l.strip() and not l.lower().startswith("gene,")]
        if W.shape[0]!=len(genes): sys.exit(f"gene count {len(genes)} != matrix {W.shape[0]}")
        niche={}
        if a.niche:
            d=pd.read_csv(a.niche); niche=dict(zip(d.iloc[:,0],d.iloc[:,1]))
        dd=build_json(W,genes,niche,a.w_thresh,a.top_nodes); dd["label"]=a.section; out["sections"][a.section]=dd
        r=dd["report"]; print(f"{a.section}: {len(dd['nodes'])} nodes, {r['n_undirected']} und, {r['n_directed']} dir"
            +(f", concordance {r['concordant']}/{r['signed_overlap']}={r['concordance']:.0%}" if r['signed_overlap'] else ""))
    json.dump(out,open(a.out,"w"),indent=2)
    print(f"\nwrote {a.out} -> upload next to perturbo_data.json")

if __name__=="__main__":
    main()
