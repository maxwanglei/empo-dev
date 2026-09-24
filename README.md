# EMPO: A Real World Evidence-Based Disease Ontology for Maternal and Pediatric Populations

This repository rebuilds the EMPO ontology in separate steps. The backbone uses the supplied MarketScan ICD-code CSV and official ICD-10-CM reference files. The current ontology covers **calendar years 2016–2021***. It retains observed codes, their required coding ancestors, and explicit maternal/pediatric population annotations.

| Step |
|---|
| 1. ICD backbone and populations |
| 2. UMLS annotations | 
| 3. MeSH integration and hierarchy |
| 4. MONDO human disease hierarchy and its ICD mappings |

This repository archive contains code, tests, guides, and the reference manifest. Input datasets and generated ontologies are separate. For Step 4, use your corrected Step 3 OWL and the supplied `mondo.owl.zip`.

## Run

Python **3.10 or newer** is sufficient. The build uses the Python standard library; no additional packages are required.

```bash
python3 build_ontology.py \
  --input data/marketscan_icd.csv \
  --references references/sources.json \
  --output-dir output

python3 -m unittest discover -s tests -v
```

An optional `--base-iri https://example.org/empo/` sets the ontology namespace. Choose a stable project namespace before integrating the output into another system.

The original Step 1 data bundle includes the exact reference snapshots needed for an offline build. A source-only repository archive does not include those snapshots: reuse that bundle or run `fetch_references.py` to obtain them. The reference manifest identifies local XML files, corresponding code lists, and their release/effective-date provenance. The relevant releases span FY2016–FY2022, including applicable interim updates, because the source observation period is defined by calendar years. Keep the manifest and reference files together when rerunning the build.

`references/sources.json` uses `schema_version`, `scope`, and `sources`. Each source identifies its `release`, inclusive `effective_from`/`effective_to` dates, official download URL, local `xml_path`/`codes_path`, and artifact checksums. Local paths are relative to the `references/` directory. The April 2020 snapshot is explicitly derived from the FY2020 base and documented amendments; its manifest also records the patch and official amendment PDF provenance.

Reference maintenance is optional for the bundled build:

```bash
python3 fetch_references.py --help
python3 fetch_references.py --verify  # Check local hashes and parsing; no network.
```

To fetch the pinned sources again and regenerate the reference manifest, run `python3 fetch_references.py` without flags. It reuses cached downloads when present and downloads missing files; network access is required for missing sources.

## Input CSV

Use the exact header `ICD10,population`. Accepted population values are `Maternal`, `0`, `1-12`, and `13-17`.

```csv
ICD10,population
O24.419,Maternal
J45.909,0
J45.909,1-12
J45.909,13-17
```

These rows illustrate the format, not study results. Rows containing the `NoDx` sentinel are excluded. Duplicate pairs of normalized full ICD code and source population are removed before grouping; a code present in several populations retains each association.

## Code grouping and hierarchy

Codes are treated as text, normalized to uppercase, and stripped of decimal points. Preserve their original characters, including zeros and `X` placeholders.

The requested grouping rule is strict: **a seven-character normalized code becomes its first six characters**. Codes of other lengths are unchanged. If the resulting six-character value ends in `X`, that `X` stays. The full original code remains in the mapping export so the grouping can be audited or revised later.

The ontology contains the grouped observed codes and all required ancestors found in the official hierarchy. Some six-character groups are not independently declared official categories. Those become explicitly labeled synthetic grouping nodes attached to a common verified official ancestor. Their existence is a project grouping decision, not a claim that the six-character string is a billable diagnosis code.

Where reference releases differ, the build uses the latest applicable hierarchy for each node and records the source provenance and conflicts. All original codes missing from the references are retained and flagged in the mapping CSV and OWL annotations. A group enters the separate unresolved branch only when it has neither an official placement nor a verified common ancestor. For groups with unrecognized members, any verified ancestor placement is supported by the recognized members only.

## Population representation

Population terms form their own hierarchy:

| Term | Parent |
|---|---|
| Maternal | SpecialPopulation |
| Pediatric | SpecialPopulation |
| Age 0 | Pediatric |
| Ages 1–12 | Pediatric |
| Ages 13–17 | Pediatric |

Code concepts link to population terms through `observedInPopulation` annotations whose values are population IRIs. A disease/code concept does not become a subclass of Maternal or Pediatric. A code may have several observed populations; this records its presence in the supplied cohorts, not population exclusivity. Population annotations are not automatically propagated to ancestors.

The CSV value `Maternal` maps to Maternal; `0`, `1-12`, and `13-17` map to the corresponding pediatric age groups. A grouped concept’s population annotation means at least one of its full-code members was observed in that population. The mapping export preserves those member-level associations.

## Outputs

| File | Purpose |
|---|---|
| `empo_icd_backbone.owl` | Fresh ontology containing code groups, required official ancestors, population terms, and annotations. |
| `code_population_mapping.csv` | Full-code-to-group mappings with the population evidence preserved. |
| `build_report.json` | Input provenance, build summary, unresolved codes, and review information. |
| `reference_conflicts.json` | Differences encountered across reference releases. |
| `reference_provenance.json` | Source releases, selected reference assertions, and provenance for the hierarchy. |
| `independent_validation.json` | Independent checks of RDF parsing, complete code–population mappings, population annotations, and hierarchy integrity. |

The mapping CSV has columns `original_icd,grouped_icd,class_iri,population,age_group,source_population,reference_status,group_kind`.

## This supplied dataset

- **60,556 original code strings** produce **36,632 observed groups** and **44,480 OWL classes**, including the required ancestors and population terms.
- **154,924 distinct code–population associations** are preserved after excluding `NoDx` rows and deduplicating.
- **1,186 original strings are unrecognized** in the supplied references. All end in `X`: 943 have six characters, 234 have five, and 9 have four. All are retained and flagged.
- **244 groups lack a verified parent** and remain in the unresolved branch. Other groups containing unrecognized members retain their documented supported placement.

A string can fit a placeholder/prefix grouping rule without being a separately declared official code or category. This explains why preserving it for analysis does not automatically produce an official reference match. **Review these placeholder strings before adding further annotations**; the build preserves the requested trailing `X` characters. See the generated reports for the complete audit; historical slide-deck totals are not reconstruction targets.

## Interpretation and next inputs

The current CSV does not identify the service date or coding release for each observation. A reference match means the code is **recognized somewhere in the 2016–2021 period**; it does not establish validity on a particular claim date or billability. The references include nonbillable categories. The latest selected hierarchy is a reconstruction view, not a historical claim-level coding assignment.

Before interpreting the population annotations, retain a written cohort definition covering:

- Delivery-identification code lists and versions, the pregnancy lookback, postpartum window if used, and enrollment rules.
- What “age 0” means in the extract, when age is measured, and how the 1–12 and 13–17 groups are assigned.
- Which years, claims settings, and diagnosis positions contribute codes, and how cohort duplicates are handled.

For a later revision, code-specific observation years or dates would allow more precise temporal checks. Exact claim-date validity requires the applicable coding-date/release information. Frequency counts can be added separately if you want prevalence-style annotations or frequency-based selection; they are not reconstructed from code presence alone. Prepare UMLS inputs only when the ICD backbone and population assignments have been reviewed and the enrichment step begins.

## Keeping the reconstruction reproducible

The private ZIP includes the supplied original CSV at `data/marketscan_icd.csv`, the code, input/reference manifest, pinned reference snapshots, and generated run outputs so the build can be rerun directly. Recorded hashes support verification of the inputs and outputs. `.gitignore` excludes the source CSV from version control. Keep this data-containing bundle private; no remote repository is published by this build.
