#!/usr/bin/env python3
"""Step 2: annotate the EMPO ICD backbone from a local MRCONSO ZIP/RRF."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path
import xml.etree.ElementTree as ET

from umls_reader import MRConsoSource, collect_anchors, iter_scoped_atoms, is_preferred_atom

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"
XML = "http://www.w3.org/XML/1998/namespace"
FIELDS = ("CUI", "LAT", "TS", "STT", "ISPREF", "AUI", "SAB", "TTY", "CODE", "STR", "SUPPRESS")
OWNED = ("umlsCUI", "umlsCandidateCUI", "umlsPreferredName", "umlsMappingStatus")
OFFICIAL_KINDS = {"chapter", "block", "code"}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_icd(value):
    value = value.strip().upper().replace(".", "")
    if not re.fullmatch(r"[A-Z][0-9][A-Z0-9]{1,5}", value):
        raise ValueError(f"Unexpected ICD code in backbone: {value!r}")
    return value


def range_identifier(node, kind, base, labels):
    """Recover the explicit range identifier from this builder's step-1 OWL."""
    iri = node.get(f"{{{RDF}}}about", "")
    if kind == "block" and iri.startswith(base + "ICD_Block_"):
        suffix = iri[len(base + "ICD_Block_"):]
        if re.fullmatch(r"[A-Z][0-9][A-Z0-9]", suffix):
            return suffix, "step1_single_code_block_iri"
        if re.fullmatch(r"[A-Z][0-9][A-Z0-9]_[A-Z][0-9][A-Z0-9]", suffix):
            return suffix.replace("_", "-"), "step1_block_iri"
    if kind == "chapter":
        ranges = set()
        for text, _ in labels:
            found = re.search(r"\(([A-Z][0-9][A-Z0-9]-[A-Z][0-9][A-Z0-9])\)\s*$", text)
            if found:
                ranges.add(found.group(1))
        if len(ranges) == 1:
            return ranges.pop(), "official_chapter_label_range"
    return "", "unavailable"


def load_backbone(path):
    tree = ET.parse(path)
    root = tree.getroot()
    if root.tag != f"{{{RDF}}}RDF":
        raise ValueError("Expected the step-1 RDF/XML OWL backbone.")
    bases = {child.tag[1:].split("}", 1)[0]
             for node in root.findall(f"{{{OWL}}}Class") for child in node
             if child.tag.endswith("}ICD_Code")}
    if len(bases) != 1:
        raise ValueError("Cannot identify the step-1 ICD annotation namespace.")
    base = bases.pop()
    if root.find(f".//{{{base}}}umlsAnnotationRelease") is not None or any(root.find(f".//{{{base}}}{name}") is not None for name in OWNED):
        raise ValueError("Input already contains UMLS annotations. Rerun against the original step-1 backbone, using a new output directory.")
    classes = root.findall(f"{{{OWL}}}Class")
    if len({n.get(f"{{{RDF}}}about") for n in classes}) != len(classes):
        raise ValueError("Input has duplicate class IRIs.")
    concepts = []
    for node in classes:
        codes = node.findall(f"{{{base}}}ICD_Code")
        kinds = node.findall(f"{{{base}}}nodeKind")
        labels = node.findall(f"{{{RDFS}}}label")
        if not codes and not any(k.text in {"chapter", "block"} for k in kinds):
            continue
        if len(kinds) != 1 or not labels or len(codes) > 1:
            raise ValueError("Expected one nodeKind and a label on each ICD hierarchy node.")
        kind = kinds[0].text
        label_values = [(label.text or "", dict(label.attrib)) for label in labels]
        if codes:
            code, origin = normalize_icd(codes[0].text or ""), "ICD_Code"
        elif kind in {"chapter", "block"}:
            code, origin = range_identifier(node, kind, base, label_values)
        else:
            raise ValueError("Official ICD code node has no ICD_Code annotation.")
        concepts.append({"element": node, "iri": node.attrib[f"{{{RDF}}}about"],
                         "code": code, "identifier_origin": origin, "kind": kind,
                         "labels": label_values})
    if not any(c["kind"] == "code" for c in concepts):
        raise ValueError("No official ICD code nodes found in this step-1 backbone.")
    return tree, base, concepts


def protected_fingerprint(root, base):
    """Hash all existing class content except the explicitly enriched fields."""
    def canonical(element):
        return (element.tag, tuple(sorted(element.attrib.items())), element.text if element.text and element.text.strip() else "", tuple(canonical(c) for c in element))
    excluded = {f"{{{RDFS}}}label", f"{{{base}}}icdPreferredName", f"{{{base}}}Synonyms"}
    excluded.update(f"{{{base}}}{name}" for name in OWNED)
    records = [(node.get(f"{{{RDF}}}about"), [canonical(c) for c in node if c.tag not in excluded])
               for node in root.findall(f"{{{OWL}}}Class")]
    return hashlib.sha256(json.dumps(records, ensure_ascii=False).encode("utf-8")).hexdigest()


def add_literal(node, namespace, name, value, attributes=None):
    attributes = attributes or {}
    tag = f"{{{namespace}}}{name}"
    if not any(c.text == value and c.attrib == attributes for c in node.findall(tag)):
        ET.SubElement(node, tag, attributes).text = value


def declare_annotations(root, base):
    descriptions = {
        "umlsCUI": "Unambiguous CUI linked through an English, non-suppressed ICD10CM atom with the same code.",
        "umlsCandidateCUI": "Candidate CUI for an ambiguous ICD10CM code match; not an accepted mapping.",
        "umlsPreferredName": "Unique English preferred string identified by TS=P, STT=PF, ISPREF=Y in the selected terms.",
        "umlsMappingStatus": "Result of exact ICD10CM source-code matching in the stated UMLS release.",
        "icdPreferredName": "Original official ICD label preserved before UMLS annotation.",
        "Synonyms": "Alternative non-suppressed English MRCONSO strings sharing the accepted CUI. These are lexical annotations, not independently verified clinical equivalences.",
        "umlsAnnotationRelease": "UMLS release supplied by the researcher for this annotation run.",
        "umlsSourceSHA256": "SHA256 of the local ZIP, RRF or gzip source file.",
        "umlsInputOWL_SHA256": "SHA256 of the unmodified step-1 ontology used as input.",
        "umlsTermProvenanceFile": "Companion CSV with atom-level CUI, AUI, source, term type, source code and release evidence.",
    }
    declared = {node.get(f"{{{RDF}}}about") for node in root.findall(f"{{{OWL}}}AnnotationProperty")}
    for name, description in descriptions.items():
        if base + name in declared:
            continue
        node = ET.SubElement(root, f"{{{OWL}}}AnnotationProperty", {f"{{{RDF}}}about": base + name})
        add_literal(node, RDFS, "label", name)
        add_literal(node, RDFS, "comment", description)


def annotate(owl_path, mrconso_path, output_dir, release="2026AA", member=None,
             keep_icd_labels=False, term_sources=None, progress=None):
    """Create a new annotated OWL and audit tables; never modify input files."""
    owl_path, mrconso_path, output_dir = map(Path, (owl_path, mrconso_path, output_dir))
    if not re.fullmatch(r"\d{4}(?:AA|AB)", release):
        raise ValueError("Release must look like 2026AA or 2025AB.")
    if term_sources is not None:
        if isinstance(term_sources, str):
            term_sources = term_sources.split(",")
        term_sources = {s.strip() for s in term_sources if s.strip()}
        if not term_sources:
            raise ValueError("Term source allowlist cannot be empty.")
    final_names = ["empo_icd_umls.owl", "umls_code_matches.csv", "umls_code_anchors.csv", "umls_terms.csv", "umls_annotation_report.json"]
    if any((output_dir / name).resolve() in {owl_path.resolve(), mrconso_path.resolve()} for name in final_names):
        raise ValueError("An output would overwrite an input file; choose another directory.")
    if any((output_dir / name).exists() for name in final_names):
        raise ValueError("Output files already exist; choose a new output directory.")
    log = progress or (lambda message: None)
    log("Reading the step-1 OWL backbone")
    tree, base, concepts = load_backbone(owl_path)
    root = tree.getroot()
    original_fingerprint = protected_fingerprint(root, base)
    original_class_iris = {n.get(f"{{{RDF}}}about") for n in root.findall(f"{{{OWL}}}Class")}
    source = MRConsoSource(mrconso_path, member=member, release=release, progress=progress)
    eligible_codes = {c["code"] for c in concepts if c["kind"] in OFFICIAL_KINDS and c["code"]}
    log(f"Pass 1/2: matching {len(eligible_codes):,} official ICD code/range identifiers to ICD10CM atoms")
    anchors = collect_anchors(source, eligible_codes)
    selected_cuis = anchors["selected_cuis"]
    log(f"Pass 2/2: collecting English terms for {len(selected_cuis):,} unambiguous CUIs")
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="umls-stage-", dir=output_dir) as staging:
        stage = Path(staging)
        db = sqlite3.connect(stage / "terms.sqlite")
        try:
            db.execute("PRAGMA journal_mode=OFF")
            columns = ", ".join(f"{name} TEXT NOT NULL" for name in FIELDS)
            db.execute(f"CREATE TABLE atoms ({columns}, UNIQUE ({', '.join(FIELDS)}))")
            sql = f"INSERT OR IGNORE INTO atoms VALUES ({','.join('?' for _ in FIELDS)})"
            batch = []
            scoped_atom_count = 0
            excluded_source_atoms = 0
            for atom in iter_scoped_atoms(source, selected_cuis):
                if term_sources is not None and atom["SAB"] not in term_sources:
                    excluded_source_atoms += 1
                    continue
                batch.append(tuple(atom[name] for name in FIELDS))
                scoped_atom_count += 1
                if len(batch) >= 5000:
                    db.executemany(sql, batch)
                    batch.clear()
            if batch:
                db.executemany(sql, batch)
            db.execute("CREATE INDEX atoms_cui ON atoms(CUI)")
            db.commit()
            unique_atoms = db.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
            declare_annotations(root, base)
            ontology = root.find(f"{{{OWL}}}Ontology")
            if ontology is None:
                raise ValueError("Input OWL lacks an ontology declaration.")
            provenance = source.provenance()
            input_hash = sha256(owl_path)
            # Source hashing is independent of claimed release metadata.
            source_hash = source.sha256
            add_literal(ontology, base, "umlsAnnotationRelease", release)
            add_literal(ontology, base, "umlsSourceSHA256", source_hash)
            add_literal(ontology, base, "umlsInputOWL_SHA256", input_hash)
            add_literal(ontology, base, "umlsTermProvenanceFile", "umls_terms.csv")
            add_literal(ontology, RDFS, "comment", f"Step 2: annotations from researcher-supplied UMLS {release}. Exact ICD10CM source-code matching; no claim that this later terminology release establishes historical coding validity. No MONDO mappings are added.")
            counts = Counter()
            report_matches = []
            match_path = stage / "umls_code_matches.csv"
            term_path = stage / "umls_terms.csv"
            anchor_path = stage / "umls_code_anchors.csv"
            term_rows = 0
            with match_path.open("w", encoding="utf-8", newline="") as match_file, term_path.open("w", encoding="utf-8", newline="") as term_file, anchor_path.open("w", encoding="utf-8", newline="") as anchor_file:
                matches = csv.writer(match_file)
                matches.writerow(["class_iri", "icd_code", "node_kind", "identifier_origin", "icd_label", "status", "accepted_cui", "candidate_cuis", "umls_preferred_name", "label_action", "synonym_count", "umls_release"])
                terms = csv.writer(term_file)
                terms.writerow(["class_iri", "icd_code", *FIELDS, "is_preferred_atom", "used_as_display_label", "umls_release"])
                anchor_writer = csv.writer(anchor_file)
                anchor_writer.writerow(["icd_code", *FIELDS, "umls_release"])
                for code, result in sorted(anchors["by_code"].items()):
                    for atom in result.get("atoms", []):
                        anchor_writer.writerow([code, *(atom[name] for name in FIELDS), release])
                for concept in sorted(concepts, key=lambda c: c["iri"]):
                    node = concept["element"]
                    code = concept["code"]
                    accepted = preferred = ""
                    candidates = []
                    synonym_count = 0
                    label_action = "kept_icd_label"
                    if concept["kind"] not in OFFICIAL_KINDS:
                        status = "skipped_" + concept["kind"]
                    elif not code:
                        status = "missing_reference_identifier"
                    else:
                        for text, attrs in concept["labels"]:
                            add_literal(node, base, "icdPreferredName", text, attrs)
                        result = anchors["by_code"][code]
                        candidates = result["cuis"]
                        if result["status"] == "ambiguous":
                            status = "ambiguous_cui"
                            for cui in candidates:
                                add_literal(node, base, "umlsCandidateCUI", cui)
                        elif result["status"] == "unmapped":
                            status = "not_found"
                        else:
                            accepted = candidates[0]
                            status = "matched"
                            add_literal(node, base, "umlsCUI", accepted)
                            rows = db.execute("SELECT " + ",".join(FIELDS) + " FROM atoms WHERE CUI=? ORDER BY STR,SAB,TTY,AUI", (accepted,)).fetchall()
                            atoms = [dict(zip(FIELDS, row)) for row in rows]
                            preferred_names = {a["STR"] for a in atoms if is_preferred_atom(a)}
                            if len(preferred_names) == 1:
                                preferred = next(iter(preferred_names))
                                add_literal(node, base, "umlsPreferredName", preferred, {f"{{{XML}}}lang": "en"})
                                if not keep_icd_labels:
                                    for label in list(node.findall(f"{{{RDFS}}}label")):
                                        node.remove(label)
                                    add_literal(node, RDFS, "label", preferred, {f"{{{XML}}}lang": "en"})
                                    label_action = "used_umls_preferred_name"
                            else:
                                label_action = "kept_icd_label_no_preferred" if not preferred_names else "kept_icd_label_ambiguous_preferred"
                                counts["matched_without_unique_preferred"] += 1
                            if not atoms:
                                counts["matched_without_allowed_terms"] += 1
                            display_names = {n.text or "" for n in node.findall(f"{{{RDFS}}}label")}
                            synonyms = sorted({a["STR"] for a in atoms} - display_names)
                            for text in synonyms:
                                add_literal(node, base, "Synonyms", text, {f"{{{XML}}}lang": "en"})
                            synonym_count = len(synonyms)
                            counts["synonym_annotations"] += synonym_count
                            for atom in atoms:
                                terms.writerow([concept["iri"], code, *(atom[name] for name in FIELDS), str(is_preferred_atom(atom)).lower(), str(atom["STR"] in display_names).lower(), release])
                                term_rows += 1
                    add_literal(node, base, "umlsMappingStatus", status)
                    counts[status] += 1
                    counts["labels_replaced"] += label_action == "used_umls_preferred_name"
                    matches.writerow([concept["iri"], code, concept["kind"], concept["identifier_origin"], " | ".join(t for t, _ in concept["labels"]), status, accepted, ";".join(candidates), preferred, label_action, synonym_count, release])
                    report_matches.append((concept["iri"], status))
            if protected_fingerprint(root, base) != original_fingerprint:
                raise ValueError("Unexpected change to existing ontology structure or population annotations.")
            for prefix, uri in [("rdf", RDF), ("rdfs", RDFS), ("owl", OWL), ("empo", base)]:
                ET.register_namespace(prefix, uri)
            ET.indent(root, space="  ")
            owl_output = stage / "empo_icd_umls.owl"
            tree.write(owl_output, encoding="utf-8", xml_declaration=True)
            # Check the completed serialized artifact and all exported concept rows.
            reparsed = ET.parse(owl_output).getroot()
            if {n.get(f"{{{RDF}}}about") for n in reparsed.findall(f"{{{OWL}}}Class")} != original_class_iris or protected_fingerprint(reparsed, base) != original_fingerprint:
                raise ValueError("Serialized ontology does not preserve the original classes and protected annotations.")
            with match_path.open(encoding="utf-8", newline="") as stream:
                actual = [(r["class_iri"], r["status"]) for r in csv.DictReader(stream)]
            if actual != report_matches:
                raise ValueError("Incomplete UMLS match table export.")
            with term_path.open(encoding="utf-8", newline="") as stream:
                if sum(1 for _ in csv.DictReader(stream)) != term_rows:
                    raise ValueError("Incomplete UMLS term table export.")
            level_by_iri = {c["iri"]: c["kind"] for c in concepts}
            report = {
                "umls_release": release, "release_is_user_supplied_metadata": True,
                "input_owl": owl_path.name, "input_owl_sha256": input_hash,
                "mrconso_file": mrconso_path.name, "mrconso_sha256": source_hash,
                "mrconso_source": provenance, "anchor_scan": anchors["stats"],
                "eligible_official_code_classes": sum(c["kind"] == "code" for c in concepts),
                "eligible_chapters": sum(c["kind"] == "chapter" and bool(c["code"]) for c in concepts),
                "eligible_blocks": sum(c["kind"] == "block" and bool(c["code"]) for c in concepts),
                "coverage_by_level": {kind: dict(Counter(status for iri, status in report_matches if level_by_iri[iri] == kind)) for kind in sorted(set(level_by_iri.values()))},
                "eligible_unique_codes": len(eligible_codes), "code_classes_in_match_table": len(concepts),
                "counts": dict(sorted(counts.items())), "distinct_unambiguous_cuis": len(selected_cuis),
                "scoped_atoms_after_source_filter": scoped_atom_count,
                "unique_stored_atoms": unique_atoms, "excluded_by_term_source_filter": excluded_source_atoms,
                "term_provenance_rows": term_rows, "term_source_allowlist": sorted(term_sources) if term_sources else "all",
                "label_policy": "keep_icd_labels" if keep_icd_labels else "unique_umls_preferred_else_icd",
                "synonym_policy": "English SUPPRESS=N strings sharing the one accepted CUI; generic lexical annotations, not asserted OWL equivalence",
                "group_policy": "Skip synthetic and unresolved groups; never pool descendant code synonyms",
                "validation": {"xml_parses": True, "all_classes_preserved": True, "hierarchy_and_population_annotations_preserved": True, "match_table_complete": True, "term_table_complete": True, "reasoner_run": False},
                "output_sha256": {name: sha256(stage / name) for name in final_names[:-1]},
            }
            (stage / "umls_annotation_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            db.close()
            db = None
            # Publish only complete, verified files. Inputs always stay untouched.
            for name in final_names:
                (stage / name).replace(output_dir / name)
            log(f"Finished: {counts['matched']:,} matched classes; {counts['synonym_annotations']:,} synonym annotations")
            return report
        finally:
            if db is not None:
                db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owl", type=Path, required=True, help="Original step-1 empo_icd_backbone.owl")
    parser.add_argument("--mrconso", type=Path, required=True, help="UMLS MRCONSO ZIP, RRF or gzip file")
    parser.add_argument("--output-dir", type=Path, default=Path("output_umls"))
    parser.add_argument("--release", default="2026AA", help="UMLS release metadata (default 2026AA)")
    parser.add_argument("--mrconso-member", help="Exact archive member when the ZIP has several MRCONSO files")
    parser.add_argument("--keep-icd-labels", action="store_true")
    parser.add_argument("--term-sources", help="Optional comma-separated SAB allowlist for terms, e.g. ICD10CM,SNOMEDCT_US,MSH")
    args = parser.parse_args()
    try:
        report = annotate(args.owl, args.mrconso, args.output_dir, release=args.release,
                          member=args.mrconso_member, keep_icd_labels=args.keep_icd_labels,
                          term_sources=args.term_sources, progress=lambda message: print(message, file=sys.stderr, flush=True))
    except (ValueError, OSError, ET.ParseError, sqlite3.Error) as exc:
        parser.exit(2, f"Annotation failed: {exc}\n")
    print(json.dumps(report["counts"], indent=2))
    print(f"Written: {args.output_dir / 'empo_icd_umls.owl'}")


if __name__ == "__main__":
    main()
