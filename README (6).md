
# Perturbo

> **An agentic AI tool for in silico gene perturbation across diseases, powered by spatial transcriptomics, causal discovery, and a Fireworks LLM backend.**

Built for the **AMD Developer Hackathon Act II, Track 3 (Unicorn)**.

---

## The product

Perturbo is an agentic web tool that lets a researcher ask a causal question about any gene or gene groups — *what happens to the disease programme when this gene is silenced?* — and get an answer grounded in real patient tissue data, within seconds.

A user opens the interface, types a gene name — or lets the agent autonomously identify the most impactful candidates — and receives:

- A **causal effect estimate**: what happens to the disease-relevant programme when this gene is knocked out, simulated from a spatial transcriptomics model trained on real tissue
- A **confidence-graded causal direction**: is this gene driving suppression of the disease programme, co-regulated by a shared niche factor, or still undetermined — with explicit reasoning behind every call
- **Clinical trial evidence**: has this gene been targeted in patients? Direct links to ClinicalTrials.gov Phase I/II/III trials

This is not a search engine over papers. It is a **causal reasoning system** built on real patient tissue, with a Fireworks LLM layer that explains what it found and why it matters.

### Two ways to work: Discover and Evaluate

Perturbo runs in two modes. In **Discover**, the researcher hands over a tissue sample and Perturbo analyses the whole ecosystem, ranking the cell populations and genes whose predicted perturbation would most strongly weaken the disease programme, so the most influential candidates surface without a manual search through thousands of genes. In **Evaluate**, the researcher brings their own candidate genes and Perturbo scores each one against the same disease model and labels it **prioritise, consider, or deprioritise**, so an in-house hypothesis can be compared directly against a target the model nominates on its own. What used to take months of computation, interpretation, and lab work is returned in minutes.

---

## Not only for one disease - a rich, swappable environment

We built and validated Perturbo on two matched pancreatic ductal adenocarcinoma (PDAC) sections, one primary tumour (T11) and one metastatic sample (HM11), because that is the disease context we work in and had matched data for. But Perturbo is not PDAC-specific, and this is central to the product, not a footnote.

The underlying environment is deliberately generic: it consumes a spatial transcriptomics object (`.h5ad`) and a driver gene table, and everything downstream — the causal coupling model, the counterfactual perturbation, the bootstrap-calibrated direction calls, the clinical trial lookup, operates on whatever tissue compartments and gene panel are provided. There is nothing pancreatic-specific hard-coded into the reasoning.

That means Perturbo can be pointed at:

- **Any cancer** with a spatial transcriptomics dataset and a meaningful compartment boundary — colorectal, lung, liver, breast, glioblastoma
- **Non-cancer diseases** with spatially structured tissue pathology — fibrosis, inflammatory bowel disease, neurodegeneration — anywhere a niche of cells is suspected to be driving or suppressing a pathological programme in a neighbouring compartment
- **Multiple platforms** — Visium, Xenium, or any spatial assay that produces a gene-by-spot expression matrix with coordinates

Swapping disease context is a data-loading step, not a redesign. This is what makes Perturbo a platform rather than a single analysis.

---

## What the user can do

- **Query any gene** in the loaded panel and get an interventional (not merely correlational) readout of its effect on the disease programme
- **Let the agent choose** — leave the gene field empty and Perturbo autonomously ranks candidates by effect size, bootstrap stability, and literature support, surfacing the ones most worth investigating
- **See the causal call and its confidence**, not a bare number — every direction comes with the bootstrap stability fraction, whether it passed multiple-testing correction, the effect size, and whether the literature agrees
- **See when the honest answer is "we don't know yet"** — pairs that don't clear the confidence bar are shown as low-confidence or as a shared latent factor, not hidden or overstated
- **Check translational relevance instantly** — a direct link to whether the gene is already in a clinical trial for the disease in question, closing the loop between a computational finding and real-world evidence
- **Swap in their own dataset** — upload a different `.h5ad` and driver table to run the entire pipeline on a different disease, cohort, or platform

---

## How the agentic system works

The web interface connects to a **Fireworks LLM backend** that orchestrates the analysis as an agentic loop:

```
User input (gene name, or nothing)
        ↓
Agent decides: use the provided gene, or autonomously identify
the highest-impact drivers from the trained coupling matrix
        ↓
Simcomen counterfactual knockout runs on the spatial model
        ↓
Causal direction module (FCI + bootstrap + FDR + effect size)
returns a confidence-graded direction for the gene pair
        ↓
Clinical trial lookup: ClinicalTrials.gov query for the gene
        ↓
LLM synthesises everything into a plain-language explanation
with explicit uncertainty and a reasoning chain
        ↓
Web interface displays the full result
```

The agentic element is real: if the user provides no gene, the system autonomously selects candidates from the trained coupling matrix based on effect size, bootstrap stability, and literature support.

---

## Methodology — how we built it

### 1. Learning the spatial gene-gene coupling — and extending it to signed couplings

We use [**Celcomen**](https://github.com/Teichlab/celcomen), an energy-based generative model from the Teichmann lab (Wellcome Sanger Institute / University of Cambridge), to learn a coupling matrix between genes from spatial transcriptomics data. Celcomen's key property — and the reason we chose it — is a theoretical identifiability guarantee: the model disentangles intra-cellular from inter-cellular gene regulation with a mean-field normalisation (`log_Z_mft`) that only holds under specific structural conditions.

**The original Celcomen coupling is unsigned** — it does not distinguish activation from suppression. For our biological question (does the stroma *suppress* the tumour, or simply co-occur with it?), sign is essential: a positive vs a negative coupling are opposite biological claims. We therefore built a **signed, sparse extension** on top of the original model — reworking the coupling normalisation to allow negative values while preserving the identifiability guarantee, then keeping only the strongest 15% of couplings for an interpretable, non-degenerate, signed gene-gene network per tissue section. This signed-sparse formulation is our contribution on top of the base Celcomen framework, not something the original model provides out of the box.

### 2. Simulating the perturbation

**Simcomen**, Celcomen's companion counterfactual module, lets us simulate a gene knockout without retraining: it relaxes the spatial model under an intervention and reads out the resulting shift in any target gene programme. This is what powers the "what happens when I silence this gene" query — a real interventional simulation, not a correlation lookup.

### 3. Establishing causal direction, honestly

A learned coupling tells you two genes interact; it does not tell you which one is upstream. For that we layered a causal discovery pipeline on top:

- **FCI** (Fast Causal Inference), which — unlike plain correlation or regression — explicitly allows for **hidden confounders**, marking a gene pair as a real directed arrow, a shared-latent-cause link, or undecided
- **Bootstrap resampling** (60 resamples) so every causal call comes with a **stability fraction** instead of a single fragile run
- **Benjamini–Hochberg FDR correction**, because with thousands of spots almost every pair looks "significant" by a raw p-value — multiple-testing correction is not optional
- **Effect size** (partial correlation, r²) alongside significance, because a statistically real but tiny effect is not what a drug discovery team should act on
- **OmniPath and KEGG** as literature priors — a direction is trusted more when the wet-lab literature agrees, and we report separately when it does not
- **Cross-section invariance** — a direction is more trustworthy when it reproduces in both the primary and the metastatic sample

Every pair receives a **graded verdict — high, medium, or low confidence** — set by how many of these independent axes agree, and each verdict carries the explicit reasons that produced it, so the call is auditable rather than a bare score. When the evidence points to a **shared niche factor driving both genes** rather than a direct arrow — which FCI records as a bidirected edge — Perturbo reports that co-regulation honestly instead of inventing a direction. A pair is only called high-confidence when several of these axes agree, never from one method alone.

### 4. Validating the pipeline before trusting it

We deliberately excluded methods that looked promising but **failed when tested on the actual data**: a joint-cumulant causal test whose statistic became numerically unstable on our skewed expression counts, a spatial-gradient orientation trick that explained only ~1–2% of variance, and dcFCI (a stronger but R-only method with no clean bridge into our Python/Windows pipeline). Every method that made it into the final tool passed a real check first.

### 5. Wrapping it for interaction

The trained coupling, the causal direction engine, and a live ClinicalTrials.gov lookup are exposed through a Fireworks-LLM-orchestrated agent that takes a gene (or nothing) and returns a synthesised, uncertainty-aware answer through the web interface described above.

---

## The scientific backbone (this repository)

The two notebooks here are the validated analysis the tool's causal engine is built on.

### `Celcomen_Experiments_and_Validation_FINAL.ipynb`

Trains Celcomen on the two matched PDAC sections, applies Simcomen counterfactual knockouts, and validates the results against a battery of noise controls.

Key validated results:
- Fibrotic driver knockout suppresses the tumour programme **~3× more** than random gene knockouts
- Niche-aware spatial readout: **AUC = 0.88**
- Directionally consistent across both sections and three random seeds
- Hop-distance analysis: effect strongest at the source, decays within one hop

Honest limits: the static cross-block signal does not reach conventional significance at Visium spot resolution (p ≈ 0.30), consistent with multi-cell averaging washing out a local niche effect. Single-cell spatial resolution (Xenium) is the natural next step.

### `Celcomen_Methodology_Models.ipynb`

Characterises the models — capacity, generalisation, and identifiability of attention-based extensions to Celcomen. Uses a spatial block validation split (not random) to remove message-passing leakage between training and held-out spots, and proves numerically that a doubly-stochastic (Sinkhorn) attention variant preserves the identifiability guarantee that makes Celcomen trustworthy in the first place.

---

## Methods summary

| Component | Method | Role |
|---|---|---|
| Spatial coupling | Celcomen (energy-based, signed, sparse keep=15%) | Identifiable G' via mean-field normalisation |
| Perturbation | Simcomen counterfactual knockout | Interventional effect without retraining |
| Causal direction | FCI + 60-resample bootstrap | Latent-confounder-aware, calibrated uncertainty |
| Multiplicity | Benjamini–Hochberg FDR (q=0.10) | Correct for multiple tested pairs |
| Effect size | Partial correlation r, r² | Separates significant-but-weak from meaningful |
| Literature priors | OmniPath ∩ KEGG | Direction confidence from two independent databases |
| Clinical context | ClinicalTrials.gov API | Translational relevance per gene |
| LLM backend | Fireworks AI | Agentic orchestration and plain-language synthesis |

---

## From target to drug-development evidence

Nominating a target is only the first step; a promising target still has to survive real drug-development evidence. Today Perturbo closes part of that loop with a live **ClinicalTrials.gov** lookup per gene, so a computational finding is checked immediately against whether the gene is already being targeted in patients. The direction we are building toward is a fuller evidence layer covering **target–disease associations, known drug–gene interactions, existing therapeutic programmes, published research, active clinical trials, and druggability** — for example, surfacing **pamrevlumab** and its clinical activity when a user evaluates **CCN2**. The point is to move a result past "this gene looks important" toward the next questions a discovery team actually asks: is it biologically relevant, is it druggable, do molecules already exist, and has it entered clinical development.

---

## Vision and roadmap

Today Perturbo converts a spatial transcriptomics sample into a prioritised, interpretable, evidence-grounded list of therapeutic targets, in minutes and in language a scientist can act on. Next we extend it to high-resolution single-cell platforms such as **Xenium**, where cells are no longer averaged inside multi-cell spots and the locality and significance that spot-level data cannot deliver have a genuine chance to appear, and we build toward **virtual target validation** — evaluating candidate targets across simulated patient cohorts before committing to wet-lab experiments. Because the underlying engine is not tied to one disease, the same approach extends across oncology, fibrosis, inflammation, and other complex multicellular conditions. Perturbo is building the in-silico front end of therapeutic target discovery: helping teams identify better hypotheses, earlier and faster.

---

## Celcomen: what we used, and its licence

Perturbo's causal engine is built directly on **[Celcomen](https://github.com/Teichlab/celcomen)** (Megas, Chen, Polanski, Asadollahzadeh, Eliasof, Schönlieb, Teichmann — Wellcome Sanger Institute / University of Cambridge), published in *Nature Communications*. We use both of its modules:

- **CCE** (Celcomen's inference module) to learn the signed gene-gene coupling
- **SCE / Simcomen** (its generative/counterfactual module) to simulate perturbations

Celcomen is distributed under the **GPL-3.0 licence**. We are using it as intended — as an installed dependency, not by copying or modifying its source into this repository — and we credit it explicitly here and in every notebook. If Perturbo is developed further as a distributed product, the GPL-3.0 terms apply to that distribution: any derivative work incorporating Celcomen's source must remain GPL-3.0 and its source must be made available. We flag this transparently because it directly shapes how the product would need to be licensed and distributed going forward.

---

## Environment

```
Python 3.11
celcomen · simcomen (Teichmann lab, GPL-3.0)
scanpy · anndata · torch · torch-geometric
causal-learn · statsmodels · omnipath
Fireworks AI API
```


---

## Team

**Eva Kourtelli, Ioulios Konstantelos, Panagiotis Lazanas**
MSc Data Science & Information Technologies
National and Kapodistrian University of Athens
AMD Developer Hackathon Act II · Track 3 (Unicorn)
