#!/usr/bin/env python3
"""
perturbo_agent_api.py  ---  Perturbo interactive agent, frontend-ready, AMD-deployable
======================================================================================
An LLM agent that goes over Perturbo's results interactively. Two modes:
  DISCOVER  -> nominate novel targets (population -> gene -> hub/upstream -> map)
  EVALUATE  -> score a user's proposed gene list (triage: prioritize / deprioritize / not-profiled)

Runs the LLM via an OpenAI-compatible endpoint. Point it at:
  * Fireworks AI  (easiest; get a key at app.fireworks.ai)         -> default
  * AMD MI300X    (vLLM/ROCm on AMD Developer Cloud)  -> set PERTURBO_LLM_BASE_URL to your MI300X server
The agent code is identical for both; only the endpoint URL + model name change.
This is the AMD story: same agent, deployable on AMD Instinct MI300X.

ENV VARS:
  PERTURBO_LLM_API_KEY    required (Fireworks key, or any token your MI300X server accepts)
  PERTURBO_LLM_BASE_URL   default https://api.fireworks.ai/inference/v1
                          for AMD: http://<your-mi300x-host>:8000/v1   (vLLM OpenAI server)
  PERTURBO_LLM_MODEL      default accounts/fireworks/models/llama-v3p1-70b-instruct
  PERTURBO_DATA           default perturbo_out/perturbo_data.json

FRONTEND ENDPOINTS (all return JSON; CORS enabled so native.builder can call them):
  GET  /health                      -> {ok, sections, has_llm}
  GET  /sections                    -> list of samples (for dropdowns)
  POST /discover  {section, n}      -> ranked target dossiers (structured, for cards/tables)
  POST /evaluate  {section, genes}  -> per-gene triage (structured, for a results table)
  POST /chat      {message, history}-> natural-language agent turn (for the chat box)
  GET  /map?section=..&population=..-> the spatial-map PNG (for <img>)

RUN:
  export PERTURBO_LLM_API_KEY="fw_..."
  python perturbo_agent_api.py                 # serves http://localhost:8000
"""
import os, json, base64
try:
    from openai import OpenAI          # openai>=1.0 (works with Fireworks + vLLM OpenAI servers)
except ImportError:
    OpenAI = None

def _default_data_path():
    if os.environ.get("PERTURBO_DATA"):
        return os.environ["PERTURBO_DATA"]
    for cand in ("data/perturbo_data.json", "perturbo_data.json", "perturbo_out/perturbo_data.json"):
        if os.path.exists(cand):
            return cand
    return "data/perturbo_data.json"
DATA_PATH = _default_data_path()
DISCLAIMER = ("Predicted, causal-given-model: prioritized in-silico hypotheses, "
              "not experimentally validated targets.")

# ============================================================
#  BRAIN  --  reads perturbo_data.json, no LLM. Pure facts.
# ============================================================
class PerturboBrain:
    def __init__(self, path=DATA_PATH):
        self.path = path
        with open(path) as f:
            self.data = json.load(f)
        self.sections = self.data["sections"]

    def _key(self, section):
        if not section:
            return next(iter(self.sections))
        s = str(section).strip().lower()
        for k, v in self.sections.items():
            if s == k.lower() or s in v.get("label", "").lower() or k.lower() in s:
                return k
        return next(iter(self.sections))

    def list_sections(self):
        return [{"id": k, "description": v.get("label", ""),
                 "n_spots": v.get("n_spots_total"),
                 "n_populations": len(v.get("populations", []))}
                for k, v in self.sections.items()]

    # ---- DISCOVER ----
    def _has_map(self, section, pop_name):
        return self.map_path(section, pop_name) is not None

    def discover(self, section=None, n=3):
        key = self._key(section); sec = self.sections[key]
        pops = sorted(sec["populations"], key=lambda p: (p.get("is_self_signal", False),
                      not p.get("clean_cross_compartment", False), p["tumour_effect"]))
        out = []
        for p in pops[:n]:
            genes = p.get("top_genes", [])[:5]
            out.append({
                "population": p["name"], "rank": p.get("rank"),
                "predicted_tumour_effect": p["tumour_effect"],
                "clean_cross_compartment": p.get("clean_cross_compartment", None),
                "shared_gene_fraction": p.get("tumour_overlap_frac"),
                "is_self_signal": p.get("is_self_signal", False),
                "well_powered": p.get("powered", True), "n_spots": p.get("n_spots"),
                "top_target_genes": [self._gene_card(g) for g in genes],
                "has_spatial_map": self._has_map(key, p["name"]),
            })
        return {"mode": "discover", "sample": key, "sample_description": sec.get("label"),
                "targets": out, "disclaimer": DISCLAIMER}

    # ---- EVALUATE ----
    def evaluate(self, genes, section=None):
        key = self._key(section); sec = self.sections[key]
        table = sec.get("gene_table", {})
        # also build a fallback lookup from populations' top_genes
        pop_lookup = {}
        for p in sec["populations"]:
            for g in p.get("top_genes", []):
                pop_lookup.setdefault(g["gene"].upper(), (g, p["name"]))
        results = []
        for raw in genes:
            gene = str(raw).strip().upper()
            if gene in {k.upper() for k in table}:
                rec = table[[k for k in table if k.upper() == gene][0]]
                results.append({"gene": gene, "profiled": True,
                                "predicted_tumour_effect": rec["effect"],
                                "verdict": self._verdict(rec["effect"], rec.get("priority_target")),
                                "population": rec.get("population"),
                                "network_hub": rec.get("is_hub"), "degree": rec.get("degree"),
                                "pathway_upstream": rec.get("is_upstream"),
                                "priority_target": rec.get("priority_target")})
            elif gene in pop_lookup:
                g, pop = pop_lookup[gene]
                results.append({"gene": gene, "profiled": True,
                                "predicted_tumour_effect": g["effect"],
                                "verdict": self._verdict(g["effect"], g.get("priority_target")),
                                "population": pop, "network_hub": g.get("is_hub"),
                                "degree": g.get("degree"), "pathway_upstream": g.get("is_upstream"),
                                "priority_target": g.get("priority_target")})
            else:
                results.append({"gene": gene, "profiled": False,
                                "verdict": "not profiled",
                                "note": "Not a niche-defining driver in this sample; Perturbo has no prediction."})
        # sort: profiled priority first, then by effect, unprofiled last
        results.sort(key=lambda r: (not r.get("profiled", False),
                                    not r.get("priority_target", False),
                                    r.get("predicted_tumour_effect", 0)))
        return {"mode": "evaluate", "sample": key, "sample_description": sec.get("label"),
                "results": results, "disclaimer": DISCLAIMER}

    def get_population(self, section, population):
        key = self._key(section)
        for p in self.sections[key]["populations"]:
            if p["name"].lower() == str(population).strip().lower():
                return {"sample": key, "population": p["name"],
                        "predicted_tumour_effect": p["tumour_effect"],
                        "clean_cross_compartment": p.get("clean_cross_compartment"),
                        "top_target_genes": [self._gene_card(g) for g in p.get("top_genes", [])],
                        "has_spatial_map": bool(p.get("spatial_map")), "disclaimer": DISCLAIMER}
        return {"error": f"population '{population}' not found in {key}"}

    def compare(self, population=None):
        """Compare a population's effect across primary vs metastasis."""
        out = {}
        for k, sec in self.sections.items():
            if population:
                for p in sec["populations"]:
                    if p["name"].lower() == str(population).strip().lower():
                        out[k] = {"label": sec.get("label"), "effect": p["tumour_effect"],
                                  "clean": p.get("clean_cross_compartment")}
            else:
                top = sorted(sec["populations"], key=lambda p: (p.get("is_self_signal", False),
                             not p.get("clean_cross_compartment", False), p["tumour_effect"]))[0]
                out[k] = {"label": sec.get("label"), "top_population": top["name"],
                          "effect": top["tumour_effect"]}
        return {"comparison": out, "disclaimer": DISCLAIMER}

    def map_path(self, section, population):
        key = self._key(section)
        base = os.path.dirname(self.path) or "."
        mapdir = os.path.join(base, "perturbo_out", "maps")
        if not os.path.isdir(mapdir):                       # fallbacks for different layouts
            for cand in [os.path.join(base, "maps"), os.path.join(base, "..", "maps"),
                         os.path.join(base, "perturbo_out/maps"), "maps", "perturbo_out/maps"]:
                if os.path.isdir(cand): mapdir = cand; break
        # 1) trust the JSON's recorded path if present
        for p in self.sections[key]["populations"]:
            if p["name"].lower() == str(population).strip().lower() and p.get("spatial_map"):
                cand = os.path.join(base, p["spatial_map"])
                if os.path.exists(cand): return cand
        # 2) construct filename from section + population (matches precompute naming)
        safe = str(population).strip().replace(" ", "_").replace("/", "_")
        for fn in [f"{key}_{safe}.png", f"{key}_{safe}.PNG"]:
            cand = os.path.join(mapdir, fn)
            if os.path.exists(cand): return cand
        # 3) last resort: fuzzy match any file starting with the section + first word of population
        if os.path.isdir(mapdir):
            first = str(population).strip().split()[0].replace("/", "_")
            for f in os.listdir(mapdir):
                if f.lower().startswith(f"{key}_{first}".lower()) and f.lower().endswith(".png"):
                    return os.path.join(mapdir, f)
        return None

    # helpers
    @staticmethod
    def _gene_card(g):
        return {"gene": g["gene"], "effect": g["effect"],
                "network_hub": g.get("is_hub", False), "degree": g.get("degree"),
                "pathway_upstream": g.get("is_upstream", False), "out_degree": g.get("out_degree"),
                "priority_target": g.get("priority_target", False)}

    @staticmethod
    def _verdict(effect, priority):
        if effect is None: return "unknown"
        if priority: return "PRIORITIZE (strong effect + network-central)"
        if effect < -0.002: return "consider (suppressive)"
        if effect < 0: return "weak (mildly suppressive)"
        return "deprioritize (no suppression predicted)"


# ============================================================
#  TOOLS  --  schema the LLM uses to call the brain
# ============================================================
TOOLS = [
    {"type": "function", "function": {
        "name": "discover_targets",
        "description": "DISCOVER mode: find and rank novel targets in a tumour sample. Returns cell "
                       "populations ranked by predicted tumour-suppressive effect, each with top target "
                       "genes (with network-hub and pathway-upstream flags). Use when the user asks what "
                       "to target, or to explore a sample.",
        "parameters": {"type": "object", "properties": {
            "section": {"type": "string", "description": "sample: 'HM11'/'metastasis' or 'T11'/'primary'"},
            "n": {"type": "integer", "description": "how many top populations (default 3)"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "evaluate_genes",
        "description": "EVALUATE mode: score a user's PROPOSED list of candidate genes. Returns each "
                       "gene's predicted tumour effect, a verdict (prioritize/consider/deprioritize), and "
                       "network flags, or 'not profiled' if Perturbo has no prediction. Use when the user "
                       "gives specific genes to assess.",
        "parameters": {"type": "object", "properties": {
            "section": {"type": "string", "description": "sample to evaluate against"},
            "genes": {"type": "array", "items": {"type": "string"},
                      "description": "candidate gene symbols, e.g. ['CCN2','EGFR','SPARC']"}},
            "required": ["genes"]}}},
    {"type": "function", "function": {
        "name": "get_population",
        "description": "Drill into one cell population: its effect, full ranked target-gene list, AND its "
                       "spatial map (the dashboard shows the map when you call this). Call this whenever the "
                       "user asks to SEE a population's spatial map or details.",
        "parameters": {"type": "object", "properties": {
            "section": {"type": "string"}, "population": {"type": "string"}},
            "required": ["population"]}}},
    {"type": "function", "function": {
        "name": "compare_primary_vs_metastasis",
        "description": "Compare a population's predicted effect (or the top target) between the primary "
                       "tumour and the liver metastasis.",
        "parameters": {"type": "object", "properties": {
            "population": {"type": "string", "description": "optional; omit for top-target comparison"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "list_sections",
        "description": "List the tumour samples Perturbo has analysed.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "lookup_gene_knowledge",
        "description": "Look up REAL external knowledge about a gene from public databases: Open Targets "
                       "(druggability/tractability + known drugs), Europe PMC (literature count + top papers), "
                       "ClinicalTrials.gov (trials), DGIdb (drug-gene interactions). Use this to contextualise "
                       "a predicted target against what's already known — is it druggable, are there existing "
                       "drugs/trials, how much literature. Every field is a real record, not model memory.",
        "parameters": {"type": "object", "properties": {
            "gene": {"type": "string", "description": "gene symbol, e.g. CCN2"},
            "disease": {"type": "string", "description": "disease context (default 'pancreatic cancer')"}},
            "required": ["gene"]}}},
]

SYSTEM_PROMPT = """You are Perturbo, an AI copilot for spatial-transcriptomics target discovery in pancreatic cancer.

You have two modes, served by tools:
- DISCOVER (discover_targets): find & rank NOVEL targets in a sample.
- EVALUATE (evaluate_genes): score a user's PROPOSED candidate genes.

Read the user's intent and call the right tool. Then explain results clearly and HONESTLY.

Rules you must follow:
- NEGATIVE tumour_effect = tumour SUPPRESSED (good target). More negative = stronger.
- 'network_hub' = central in the learned coupling network (data-derived): a strong, honest claim.
- 'pathway_upstream' = directs downstream genes per curated pathways: direction is IMPORTED from
  databases, NOT learned from the data -- always say so when you mention it.
- 'clean_cross_compartment' = genuine stroma->tumour effect (not epithelial self-signal). Prefer these.
- Populations flagged is_self_signal or high shared_gene_fraction are NOT clean targets -- say so.
- ALWAYS carry the caveat: these are PREDICTED, causal-given-model hypotheses requiring lab validation.
- NEVER invent genes, numbers, or effects. Only report what the tools return. If a gene is 'not profiled', say so plainly.
- SPATIAL MAPS: the dashboard you are driving DISPLAYS spatial maps automatically next to each target
  (a "spatial map" button on each population, and a framed map when drilling into a population). So when
  a user asks to see a map, tell them it's shown on the right / click the population's map button to view
  and zoom it. Do NOT say you cannot display images -- the interface shows them; you just describe what they show.
- EXTERNAL KNOWLEDGE: when a user asks whether a gene is druggable, known, in trials, or what the
  literature says, call lookup_gene_knowledge -- it returns REAL records (Open Targets, Europe PMC,
  ClinicalTrials.gov, DGIdb). Report only what it returns; never invent drugs, trials, or paper counts.
  Distinguish clearly: Perturbo's prediction is in-silico; the database facts are external evidence.
- Be concise, specific, and useful. Lead with the answer (the target/verdict), then the caveat."""


# ============================================================
#  AGENT  --  the LLM tool-calling loop
# ============================================================
class PerturboAgent:
    def __init__(self, brain, api_key=None, base_url=None, model=None):
        if OpenAI is None:
            raise RuntimeError("pip install openai")
        self.brain = brain
        self.model = model or os.environ.get(
            "PERTURBO_LLM_MODEL", "accounts/fireworks/models/llama-v3p1-70b-instruct")
        self.client = OpenAI(
            api_key=api_key or os.environ.get("PERTURBO_LLM_API_KEY", "EMPTY"),
            base_url=base_url or os.environ.get(
                "PERTURBO_LLM_BASE_URL", "https://api.fireworks.ai/inference/v1"))

    def _dispatch(self, name, args):
        b = self.brain
        try:
            if name == "discover_targets":   return b.discover(args.get("section"), args.get("n", 3))
            if name == "evaluate_genes":      return b.evaluate(args["genes"], args.get("section"))
            if name == "get_population":      return b.get_population(args.get("section"), args["population"])
            if name == "compare_primary_vs_metastasis": return b.compare(args.get("population"))
            if name == "list_sections":       return b.list_sections()
            if name == "lookup_gene_knowledge":
                try:
                    from gene_knowledge import lookup_gene_knowledge
                    return lookup_gene_knowledge(args["gene"], args.get("disease", "pancreatic cancer"))
                except Exception as e:
                    return {"error": f"knowledge lookup unavailable: {e}"}
            return {"error": f"unknown tool {name}"}
        except Exception as e:
            return {"error": str(e)}

    def ask(self, user_text, history=None):
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
        if history: msgs += history
        msgs.append({"role": "user", "content": user_text})
        directives = []                      # what visuals to show, inferred from tool calls
        for _ in range(6):
            resp = self.client.chat.completions.create(
                model=self.model, messages=msgs, tools=TOOLS, temperature=0.2)
            m = resp.choices[0].message
            if not m.tool_calls:
                v = directives[-1] if directives else None
                return {"reply": m.content, "visual": v, "visuals": directives,
                        "script": self.build_script(v, m.content),
                        "history": msgs[1:] + [{"role": "assistant", "content": m.content}]}
            msgs.append({"role": "assistant", "content": m.content or "", "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in m.tool_calls]})
            for tc in m.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                result = self._dispatch(tc.function.name, args)
                # record a visual directive the frontend can act on
                directives.append(self._visual_for(tc.function.name, args, result))
                msgs.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result)[:6000]})
        return {"reply": "(stopped after several tool calls)", "visual": (directives[-1] if directives else None),
                "visuals": directives, "history": msgs[1:]}

    @staticmethod
    def _visual_for(tool, args, result):
        """Map a tool call to a dashboard directive: what chart to animate in, with data."""
        if tool == "discover_targets":
            return {"type": "ranking", "section": result.get("sample"),
                    "sample_description": result.get("sample_description"),
                    "targets": result.get("targets", [])}
        if tool == "evaluate_genes":
            return {"type": "evaluate", "section": result.get("sample"),
                    "results": result.get("results", [])}
        if tool == "get_population":
            return {"type": "genes", "section": result.get("sample"),
                    "population": result.get("population"),
                    "genes": result.get("top_target_genes", [])}
        if tool == "compare_primary_vs_metastasis":
            return {"type": "compare", "comparison": result.get("comparison", {})}
        if tool == "lookup_gene_knowledge":
            return {"type": "knowledge", "gene": result.get("gene"), "knowledge": result}
        return None

    @staticmethod
    def build_script(v, reply):
        """Turn a visual directive into a PRESENTATION SCRIPT: an ordered list of
        {say, action} steps the frontend plays one-by-one (typewriter narration + synced UI action).
        Deterministic so it always plays cleanly; narration is derived from the real data."""
        if not v:
            return [{"say": reply, "action": {"type": "none"}}]
        steps = []
        def f(e): return ("" if e is None else (("" if e < 0 else "+") + format(e, ".5f")))

        if v["type"] == "ranking":
            desc = v.get("sample_description") or v.get("section")
            ts = v.get("targets", [])
            for t in ts: t["_section"] = v.get("section")
            steps.append({"say": f"Let's look at the {desc}. I'll rank every cell population by how much silencing it is predicted to suppress the tumour.",
                          "action": {"type": "title", "kicker": desc, "title": "Target populations"}})
            steps.append({"say": "Here is the full ranking — more negative means stronger predicted suppression.",
                          "action": {"type": "chart", "id": "main",
                                     "labels": [t["population"] for t in ts],
                                     "data": [round(t["predicted_tumour_effect"]*1000, 3) for t in ts],
                                     "colors": ["#5F6E80" if t.get("is_self_signal") else ("#5BB3A6" if t.get("clean_cross_compartment") else "#DBAE5F") for t in ts]}})
            steps.append({"say": "Now the individual targets, strongest first.",
                          "action": {"type": "open_targets"}})
            for i, t in enumerate(ts):
                if i == 0 and not t.get("is_self_signal"):
                    gtxt = ", ".join(g["gene"] for g in t.get("top_target_genes", [])[:3])
                    say = f"{t['population']} is the top target — predicted effect {f(t['predicted_tumour_effect'])}, a clean cross-compartment signal. Its leading genes are {gtxt}."
                else:
                    tag = "self-signal (not a stromal target)" if t.get("is_self_signal") else ("clean cross-compartment" if t.get("clean_cross_compartment") else "flagged — shares genes with the tumour")
                    say = f"{t['population']}: effect {f(t['predicted_tumour_effect'])}, {tag}."
                steps.append({"say": say, "action": {"type": "target", "target": t, "index": i, "top": (i == 0 and not t.get("is_self_signal"))}})
                if i == 0 and not t.get("is_self_signal") and t.get("has_spatial_map"):
                    steps.append({"say": f"Here is where knocking out {t['population']} is predicted to act across the tissue — the blue regions are where the tumour programme is suppressed.",
                                  "action": {"type": "map", "section": v.get("section"), "population": t["population"], "hero": True}})
                if i == 0 and not t.get("is_self_signal") and t.get("top_target_genes"):
                    steps.append({"say": f"And this is the coupling network for {t['population']} — node size is each gene's connectivity in the learned network, amber marks the hubs, and the edges show each gene's predicted effect on the tumour.",
                                  "action": {"type": "network", "section": v.get("section"), "population": t["population"]}})
            # (A) gallery: spatial maps for EVERY clean target with a map
            gallery = [{"section": v.get("section"), "population": t["population"]}
                       for t in ts if t.get("has_spatial_map") and t.get("clean_cross_compartment") and not t.get("is_self_signal")]
            if len(gallery) > 1:
                names = ", ".join(g["population"] for g in gallery)
                steps.append({"say": f"And here is the full spatial atlas — where each target population acts across the tumour: {names}. Each map is a predicted knockout effect; click any to zoom.",
                              "action": {"type": "gallery", "maps": gallery}})
            steps.append({"say": reply, "action": {"type": "summary"}})

        elif v["type"] == "evaluate":
            steps.append({"say": "Let me score the genes you proposed against the model.",
                          "action": {"type": "title", "kicker": v.get("section") or "", "title": "Target evaluation"}})
            steps.append({"say": "Here is the triage — prioritise, consider, deprioritise, or not-profiled.",
                          "action": {"type": "table", "results": v.get("results", [])}})
            steps.append({"say": reply, "action": {"type": "summary"}})

        elif v["type"] == "genes":
            pop = v.get("population")
            steps.append({"say": f"Let's drill into {pop}.",
                          "action": {"type": "title", "kicker": v.get("section") or "", "title": f"{pop} — target genes"}})
            gs = v.get("genes", [])
            steps.append({"say": "Its driver genes, ranked by predicted effect.",
                          "action": {"type": "chart", "id": "main",
                                     "labels": [g["gene"] for g in gs[:10]],
                                     "data": [round(g["effect"]*1000, 3) for g in gs[:10]],
                                     "colors": ["#E6A85C" if g.get("priority_target") else ("#EF8F6E" if g["effect"] < 0 else "#5BB3A6") for g in gs[:10]]}})
            if v.get("section") and pop:
                steps.append({"say": f"And the spatial map for {pop}.",
                              "action": {"type": "map", "section": v.get("section"), "population": pop}})
            if gs:
                steps.append({"say": f"Here's the coupling network — node size is connectivity, amber are hubs, edges show predicted effect.",
                              "action": {"type": "network", "section": v.get("section"), "population": pop}})
            steps.append({"say": reply, "action": {"type": "summary"}})

        elif v["type"] == "compare":
            comp = v.get("comparison", {})
            keys = list(comp.keys())
            steps.append({"say": "Comparing across the two samples.",
                          "action": {"type": "title", "kicker": "Cross-sample", "title": "Primary vs metastasis"}})
            steps.append({"say": "The predicted effect side by side.",
                          "action": {"type": "chart", "id": "main",
                                     "labels": [comp[k].get("label", k) for k in keys],
                                     "data": [round((comp[k].get("effect", 0))*1000, 3) for k in keys],
                                     "colors": ["#EF8F6E", "#5BB3A6"]}})
            steps.append({"say": reply, "action": {"type": "summary"}})

        elif v["type"] == "knowledge":
            k = v.get("knowledge", {}); gene = v.get("gene")
            steps.append({"say": f"Let me check what's already known about {gene} in the public databases.",
                          "action": {"type": "title", "kicker": "external evidence", "title": f"{gene} — known biology & druggability"}})
            steps.append({"say": k.get("summary", ""), "action": {"type": "knowledge", "knowledge": k}})
            steps.append({"say": reply, "action": {"type": "summary"}})
        else:
            steps.append({"say": reply, "action": {"type": "none"}})
        return steps


# ============================================================
#  FLASK API  --  frontend-facing endpoints (CORS enabled)
# ============================================================
def make_app(brain, agent=None, static_dir=None):
    from flask import Flask, request, jsonify, send_file, send_from_directory
    app = Flask(__name__)

    # ---- CORS on EVERY response (incl. errors + preflight) ----
    @app.after_request
    def cors(r):
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        r.headers["Access-Control-Max-Age"] = "86400"
        return r

    # ---- answer ALL preflight OPTIONS (a JSON POST triggers OPTIONS /chat etc.) ----
    @app.route("/<path:_any>", methods=["OPTIONS"])
    @app.route("/", methods=["OPTIONS"])
    def _preflight(_any=None):
        return ("", 204)

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "sections": list(brain.sections.keys()),
                        "has_llm": agent is not None})

    @app.get("/sections")
    def sections():
        return jsonify(brain.list_sections())

    @app.post("/discover")
    def discover():
        try:
            d = request.get_json(force=True, silent=True) or {}
            return jsonify(brain.discover(d.get("section"), int(d.get("n", 3))))
        except Exception as e:
            return jsonify({"error": str(e)}), 200

    @app.post("/evaluate")
    def evaluate():
        try:
            d = request.get_json(force=True, silent=True) or {}
            genes = d.get("genes") or []
            if isinstance(genes, str):
                genes = [g.strip() for g in genes.replace(",", " ").split() if g.strip()]
            return jsonify(brain.evaluate(genes, d.get("section")))
        except Exception as e:
            return jsonify({"error": str(e)}), 200

    @app.post("/chat")
    def chat():
        # NEVER crash: always return JSON (with CORS) so the dashboard stays live.
        d = request.get_json(force=True, silent=True) or {}
        msg = d.get("message", ""); hist = d.get("history")
        if agent is None:
            return jsonify({"reply": "The analysis engine is running, but the language model isn't "
                                     "configured on this server (no API key). Data endpoints still work.",
                            "script": None, "history": hist or []}), 200
        try:
            return jsonify(agent.ask(msg, hist))
        except Exception as e:
            return jsonify({"reply": f"(the agent hit an error handling that: {e})",
                            "script": None, "history": hist or []}), 200

    @app.post("/knowledge")
    def knowledge():
        try:
            from gene_knowledge import lookup_gene_knowledge
            d = request.get_json(force=True, silent=True) or {}
            return jsonify(lookup_gene_knowledge(d.get("gene", ""), d.get("disease", "pancreatic cancer")))
        except Exception as e:
            return jsonify({"error": str(e)}), 200

    @app.get("/network")
    def network():
        base = os.path.dirname(brain.path) or "."
        for cand in [os.path.join(base, "network.json"), "network.json",
                     os.path.join(base, "..", "network.json"),
                     "/workspace/network.json"]:
            if os.path.exists(cand):
                with open(cand) as f: return jsonify(json.load(f))
        return jsonify({"error": "network.json not found", "sections": {}}), 200

    @app.get("/map")
    def serve_map():
        try:
            p = brain.map_path(request.args.get("section"), request.args.get("population"))
            if p and os.path.exists(p):
                return send_file(p, mimetype="image/png")
        except Exception as e:
            return jsonify({"error": str(e)}), 404
        return jsonify({"error": "map not found"}), 404

    # ---- OPTIONAL: serve the dashboard same-origin (removes CORS + URL problems entirely) ----
    # put perturbo_dashboard.html (and any assets) in static_dir; then open {URL}/ directly.
    if static_dir and os.path.isdir(static_dir):
        @app.get("/")
        def _index():
            for name in ["index.html", "perturbo_dashboard.html"]:
                if os.path.exists(os.path.join(static_dir, name)):
                    return send_from_directory(static_dir, name)
            return jsonify({"error": "no index.html/perturbo_dashboard.html in static dir"}), 404
        @app.get("/<path:f>")
        def _static(f):
            if os.path.exists(os.path.join(static_dir, f)):
                return send_from_directory(static_dir, f)
            return jsonify({"error": "not found"}), 404

    return app


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", action="store_true", help="chat in terminal instead of serving")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--static", default=None,
                    help="dir with perturbo_dashboard.html to serve SAME-ORIGIN (removes CORS+URL issues)")
    args = ap.parse_args()

    brain = PerturboBrain()
    print(f"Perturbo brain loaded: sections {list(brain.sections.keys())}")
    agent = None
    if os.environ.get("PERTURBO_LLM_API_KEY"):
        try:
            agent = PerturboAgent(brain)
            print(f"LLM agent ready: model={agent.model}")
            print(f"  endpoint={agent.client.base_url}  (swap PERTURBO_LLM_BASE_URL for AMD MI300X)")
        except Exception as e:
            print(f"LLM not ready ({e}); serving data endpoints only")
    else:
        print("No PERTURBO_LLM_API_KEY set -> /discover and /evaluate work; /chat disabled")

    if args.cli:
        if not agent: print("need PERTURBO_LLM_API_KEY for --cli"); return
        print("\nPerturbo CLI. Ask about targets, or paste genes to evaluate. Ctrl-C to exit.\n")
        hist = []
        while True:
            try: q = input("you> ").strip()
            except (EOFError, KeyboardInterrupt): print(); break
            if not q: continue
            r = agent.ask(q, hist); hist = r["history"][-8:]
            print("\nperturbo>", r["reply"], "\n")
    else:
        app = make_app(brain, agent, static_dir=args.static)
        print(f"\nServing on http://0.0.0.0:{args.port}  (endpoints: /health /sections /discover /evaluate /chat /map /network)")
        if args.static:
            print(f"  same-origin dashboard: open  {{proxy-url}}/  (serving {args.static})  -> no CORS, no URL to update")
        app.run(host="0.0.0.0", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
