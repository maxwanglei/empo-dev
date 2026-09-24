Place your source file here as `marketscan_icd.csv`, with columns `ICD10,population`.
The source CSV is excluded from version control. Keep the original unmodified.

Run from the repository root:

```bash
python3 build_ontology.py --input data/marketscan_icd.csv --output-dir output
```
