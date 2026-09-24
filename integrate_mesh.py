#!/usr/bin/env python3
"""Add MeSH headings, their native hierarchy, and audited ICD mapping links.

Python 3.10+, standard library only. Input files are never modified.
Unreviewed mapping labels never generate logical subclass/equivalence axioms.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET

from mesh_reader import read_mesh

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"
XML = "http://www.w3.org/XML/1998/namespace"
MESH = "http://id.nlm.nih.gov/mesh/"
ABOUT, RESOURCE = f"{{{RDF}}}about", f"{{{RDF}}}resource"
CLASS, SUBCLASS = f"{{{OWL}}}Class", f"{{{RDFS}}}subClassOf"
SUBJECT_COMMENT = (
    "MeSH descriptors are represented here as subject categories. Their native broader-heading "
    "relationships are projected as OWL subclass links for navigation under MedicalHeading; "
    "this is not an assertion that every heading is a disease or that the MeSH tree is a "
    "disease is-a hierarchy. Native meshBroader links and every tree position are retained. "
    "Do not combine this subject-category projection with ICD-to-MeSH disease subclass or "
    "equivalence axioms; use mapping annotations for those cross-vocabulary links."
)
OUTPUTS = (
    "empo_icd_umls_mesh.owl", "mesh_descriptors.csv", "mesh_hierarchy.csv",
    "mesh_concepts.jsonl", "icd_mesh_mappings.csv", "icd_mesh_row_audit.csv",
    "icd_mesh_coverage.csv", "mapping_review_template.csv", "mesh_integration_report.json",
)
REVIEW_FIELDS = ("icd_code", "mesh_id", "decision", "reviewer", "evidence", "class_interpretation")
MAPPING_FIELDS = (
    "icd_code", "mesh_id", "icd_class_iri", "icd_label", "mesh_label",
    "mapping_types", "original_icd_codes", "source_levels", "source_row_count",
    "direct_original_rows", "different_original_rows", "status", "issues",
    "review_decision", "reviewer", "evidence", "class_interpretation",
)


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def norm_icd(text):
    """Normalize display format only. Never truncate or use a prefix match."""
    return text.strip().upper().replace(".", "")


def valid_icd(code):
    return bool(re.fullmatch(r"[A-Z][0-9][A-Z0-9]{1,5}", code))


def literal(node, base, name, text, english=False):
    attributes = {f"{{{XML}}}lang": "en"} if english else {}
    ET.SubElement(node, f"{{{base}}}{name}", attributes).text = str(text)


def link(node, base, name, iri):
    ET.SubElement(node, f"{{{base}}}{name}", {RESOURCE: iri})


def hierarchy_targets(descriptors, parents, base, mode="subject-categories"):
    """Return exact immediate class parents, retaining root occurrences and polyhierarchy."""
    if mode not in {"subject-categories", "annotations"}:
        raise ValueError("mesh_hierarchy must be subject-categories or annotations.")
    result = {}
    for identifier, descriptor in descriptors.items():
        if mode == "annotations":
            targets = {base + "MedicalHeading"}
        else:
            targets = {MESH + parent for parent in parents[identifier]}
            if not targets or any("." not in position for position in descriptor["tree_numbers"]):
                targets.add(base + "MedicalHeading")
        result[identifier] = targets
    return result


def annotate_hierarchy(ontology, container, descriptor_nodes, base, mode):
    literal(ontology, base, "meshHierarchyMode", mode)
    if mode == "subject-categories":
        literal(ontology, RDFS, "comment", SUBJECT_COMMENT)
        literal(container, RDFS, "comment", SUBJECT_COMMENT)
        for node in (ontology, container, *descriptor_nodes):
            literal(node, base, "meshClassInterpretation", "subject_categories")


def signature(element):
    """Whitespace between XML elements is not RDF content."""
    text = element.text or ""
    return (element.tag, tuple(sorted(element.attrib.items())),
            text if text.strip() else "", tuple(signature(c) for c in element))


def load_input(path):
    tree = ET.parse(path)
    root = tree.getroot()
    if root.tag != f"{{{RDF}}}RDF":
        raise ValueError("Expected RDF/XML from the EMPO ICD backbone builder.")
    classes = root.findall(CLASS)
    bases = {c.tag[1:].split("}", 1)[0] for n in classes for c in n
             if c.tag.endswith("}ICD_Code")}
    if len(bases) != 1:
        raise ValueError("Cannot identify a unique ICD annotation namespace.")
    base = bases.pop()
    if root.find(f".//{{{base}}}meshAnnotationRelease") is not None:
        raise ValueError("Input already has MeSH annotations. Rerun from the original ICD/UMLS input.")
    by_iri = {n.get(ABOUT): n for n in classes}
    if None in by_iri or len(by_iri) != len(classes):
        raise ValueError("Input contains unnamed or duplicate OWL classes.")
    if base + "MedicalHeading" in by_iri or any(iri.startswith(MESH) for iri in by_iri):
        raise ValueError("Input already contains a MeSH/MedicalHeading branch.")
    if any(root.find(f".//{{{base}}}{p}") is not None for p in ("Has_MeSH", "candidateMeSH", "meshBroader")):
        raise ValueError("Input already has MeSH links; use the original ICD/UMLS input.")
    by_code = {}
    all_icd = []
    for iri, node in by_iri.items():
        kind = node.findtext(f"{{{base}}}nodeKind", "")
        codes = node.findall(f"{{{base}}}ICD_Code")
        if len(codes) > 1:
            raise ValueError(f"Multiple ICD_Code annotations on {iri}.")
        code = norm_icd(codes[0].text or "") if codes else ""
        if kind in {"code", "chapter", "block", "analytical_group", "unresolved_group"}:
            all_icd.append((iri, code, kind))
        if kind == "code":
            if not valid_icd(code) or code in by_code:
                raise ValueError(f"Invalid or ambiguous official ICD code: {code!r}.")
            by_code[code] = node
    if not by_code:
        raise ValueError("No official ICD code nodes found.")
    ontology = root.find(f"{{{OWL}}}Ontology")
    if ontology is None:
        raise ValueError("Input has no owl:Ontology declaration.")
    return tree, base, by_iri, by_code, all_icd, ontology


def read_mapping(path):
    rows, grouped = [], {}
    required = {"ICD", "level", "value", "mesh", "mapping_type"}
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("Mapping CSV requires ICD,level,value,mesh,mapping_type columns.")
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError("Mapping CSV has duplicate headers.")
        for number, record in enumerate(reader, 2):
            if None in record or any(record.get(k) is None for k in required):
                raise ValueError(f"Malformed mapping CSV row {number}.")
            code, mesh = norm_icd(record["value"]), record["mesh"].strip().upper()
            raw = {k: record[k] for k in ("ICD", "level", "value", "mesh", "mapping_type")}
            raw.update(source_row=number, icd_code=code, mesh_id=mesh)
            rows.append(raw)
            key = (code, mesh)
            if key not in grouped:
                grouped[key] = {"mapping_types": set(), "original_icd_codes": set(),
                                "source_levels": set(), "source_rows": [], "direct_original_rows": 0}
            item = grouped[key]
            item["mapping_types"].add(record["mapping_type"])
            item["original_icd_codes"].add(record["ICD"])
            item["source_levels"].add(record["level"])
            item["source_rows"].append(number)
            item["direct_original_rows"] += norm_icd(record["ICD"]) == code
    if not rows:
        raise ValueError("Mapping CSV is empty.")
    return rows, grouped


def read_reviews(path, pairs):
    reviews = {}
    if path is None:
        return reviews
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not set(REVIEW_FIELDS[:-1]).issubset(reader.fieldnames):
            raise ValueError("Review CSV requires icd_code,mesh_id,decision,reviewer,evidence columns.")
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError("Review CSV has duplicate headers.")
        for number, raw in enumerate(reader, 2):
            if None in raw or any(raw.get(k) is None for k in REVIEW_FIELDS[:-1]):
                raise ValueError(f"Malformed review CSV row {number}.")
            row = {k: (raw.get(k) or "").strip() for k in REVIEW_FIELDS}
            if not row["decision"]:
                continue
            key = (norm_icd(row["icd_code"]), row["mesh_id"].upper())
            if key not in pairs:
                raise ValueError(f"Review row {number} refers to an unknown mapping pair: {key}.")
            if key in reviews:
                raise ValueError(f"Duplicate review for mapping pair {key}.")
            if row["decision"] not in {"reject", "accept_link", "subclass"}:
                raise ValueError(f"Unknown review decision on row {number}: {row['decision']!r}.")
            if not row["reviewer"] or not row["evidence"]:
                raise ValueError(f"Review row {number} needs reviewer and evidence.")
            if row["decision"] == "subclass" and row["class_interpretation"] != "disease_concepts":
                raise ValueError("Subclass approval requires class_interpretation=disease_concepts.")
            reviews[key] = row
    return reviews


def declare_properties(root, base):
    descriptions = {
        "meshDescriptorID": "Official MeSH descriptor identifier.",
        "meshTreeNumber": "Tree position in the pinned MeSH descriptor release. Multiple positions are retained.",
        "meshEntryTerm": "Alternative term within the descriptor's preferred MeSH concept, retained as entry vocabulary.",
        "meshScopeNote": "Scope note of the descriptor's preferred MeSH concept.",
        "meshBroader": "Immediate broader MeSH descriptor derived from tree positions; an annotation link, not an OWL disease subclass assertion.",
        "meshRelease": "Pinned MeSH release for this descriptor.",
        "candidateMeSH": "Unreviewed mapping proposal from the supplied CSV. Does not assert equivalence or subsumption.",
        "Has_MeSH": "Mapping accepted by an explicit review record; does not alone assert equivalence or subsumption.",
        "meshMappingStatus": "Mapping review status on an annotated link axiom.",
        "meshMappingType": "Original mapping_type label, with no inferred logical semantics.",
        "meshMappingRow": "Row number in the original mapping CSV (header is row 1).",
        "meshMappingReviewer": "Reviewer identifier exactly as supplied; may identify source review rather than human clinical validation.",
        "meshMappingEvidence": "Review justification and evidence exactly as supplied.",
        "meshClassInterpretation": "Declared class interpretation: subject_categories for the navigation projection; disease_concepts only for a reviewed disease subclass axiom in annotations mode.",
        "meshHierarchyMode": "subject-categories projects native MeSH broader relationships into the visible OWL class hierarchy; annotations retains a flat MeSH class branch.",
        "meshHierarchyFixInputSHA256": "SHA256 of the unchanged Step 3 OWL before the visible MeSH hierarchy correction.",
        "meshMappingSourceSHA256": "SHA256 of the provided mapping CSV.",
        "meshAnnotationRelease": "Pinned MeSH descriptor release used by this integration.",
        "meshSourceSHA256": "SHA256 of the supplied MeSH XML archive/file.",
        "meshInputOWL_SHA256": "SHA256 of the untouched input OWL.",
        "meshReviewSHA256": "SHA256 of optional review CSV.",
        "meshMappingProvenanceFile": "Companion row-level mapping audit file.",
    }
    existing = {n.get(ABOUT) for n in root.findall(f"{{{OWL}}}AnnotationProperty")}
    for name, comment in descriptions.items():
        if base + name in existing:
            continue
        node = ET.SubElement(root, f"{{{OWL}}}AnnotationProperty", {ABOUT: base + name})
        literal(node, RDFS, "label", name)
        literal(node, RDFS, "comment", comment)


def axiom(root, base, source, predicate, target, status, mapping, review, source_hash):
    node = ET.SubElement(root, f"{{{OWL}}}Axiom")
    link(node, OWL, "annotatedSource", source)
    link(node, OWL, "annotatedProperty", predicate)
    link(node, OWL, "annotatedTarget", target)
    literal(node, base, "meshMappingStatus", status)
    literal(node, base, "meshMappingSourceSHA256", source_hash)
    for label in sorted(mapping["mapping_types"]):
        literal(node, base, "meshMappingType", label)
    for number in mapping["source_rows"]:
        literal(node, base, "meshMappingRow", number)
    if review:
        for key, name in (("reviewer", "meshMappingReviewer"), ("evidence", "meshMappingEvidence"),
                          ("class_interpretation", "meshClassInterpretation")):
            if review[key]:
                literal(node, base, name, review[key])


def write_csv(path, fields, rows):
    count = 0
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def validate_serialized(path, baseline, base, expected_mesh_ids, expected_parents, expected_links, expected_subclasses,
                        expected_mesh_subclasses=None, mesh_hierarchy="annotations"):
    """Stream verification of all original class contents and exact added edges."""
    original_seen, mesh_seen, broader, links_seen, subclasses_seen = set(), set(), set(), set(), set()
    root_seen = False
    classes_seen = set()
    for event, node in ET.iterparse(path, events=("end",)):
        if node.tag != CLASS:
            continue
        iri = node.get(ABOUT)
        if iri in classes_seen:
            raise ValueError("Serialized output has duplicate classes.")
        classes_seen.add(iri)
        if iri in baseline:
            original_seen.add(iri)
            old_attrs, old_children = baseline[iri]
            if tuple(sorted(node.attrib.items())) != old_attrs:
                raise ValueError("An original class attribute changed.")
            children = list(node)
            if tuple(signature(c) for c in children[:len(old_children)]) != old_children:
                raise ValueError("An original ICD/UMLS/population class changed.")
            for child in children[len(old_children):]:
                target = child.get(RESOURCE)
                if child.tag in {f"{{{base}}}candidateMeSH", f"{{{base}}}Has_MeSH"}:
                    links_seen.add((iri, child.tag, target))
                elif child.tag == SUBCLASS:
                    subclasses_seen.add((iri, target))
                else:
                    raise ValueError("Unexpected added annotation on an original class.")
        elif iri == base + "MedicalHeading":
            root_seen = True
        elif iri and iri.startswith(MESH) and iri[len(MESH):] in expected_mesh_ids:
            mesh_seen.add(iri[len(MESH):])
            expected_targets = (expected_mesh_subclasses or {}).get(iri[len(MESH):], {base + "MedicalHeading"})
            targets = [c.get(RESOURCE) for c in node.findall(SUBCLASS)]
            if set(targets) != expected_targets or len(targets) != len(expected_targets):
                raise ValueError("Unexpected MeSH subclass assertion.")
            if mesh_hierarchy == "subject-categories" and node.findtext(f"{{{base}}}meshClassInterpretation") != "subject_categories":
                raise ValueError("Missing MeSH subject-category interpretation.")
            for child in node.findall(f"{{{base}}}meshBroader"):
                broader.add((iri, child.get(RESOURCE)))
        else:
            raise ValueError(f"Unexpected new class {iri}.")
        node.clear()
    if original_seen != set(baseline) or mesh_seen != expected_mesh_ids or not root_seen:
        raise ValueError("Incomplete class serialization.")
    if broader != expected_parents or links_seen != expected_links or subclasses_seen != expected_subclasses:
        raise ValueError("Serialized mapping/hierarchy edges differ from planned output.")


def integrate(owl_path, mesh_path, mapping_path, output_dir, *, mesh_release="2025",
              member=None, review_csv=None, progress=None, mesh_hierarchy="subject-categories"):
    """Integrate supplied sources and atomically publish a new output directory."""
    owl_path, mesh_path, mapping_path, output_dir = map(Path, (owl_path, mesh_path, mapping_path, output_dir))
    review_csv = Path(review_csv) if review_csv is not None else None
    if mesh_hierarchy not in {"subject-categories", "annotations"}:
        raise ValueError("mesh_hierarchy must be subject-categories or annotations.")
    if output_dir.exists():
        raise ValueError("Output directory already exists. Use a fresh output directory.")
    inputs = [owl_path, mesh_path, mapping_path] + ([review_csv] if review_csv else [])
    input_hashes = {str(p.resolve()): sha256(p) for p in inputs}
    log = progress or (lambda message: None)
    log("Reading original ICD/UMLS ontology and mapping evidence")
    tree, base, original_classes, codes, all_icd, ontology = load_input(owl_path)
    root = tree.getroot()
    if mesh_hierarchy == "subject-categories":
        for node in original_classes.values():
            for expression in node.findall(SUBCLASS) + node.findall(f"{{{OWL}}}equivalentClass"):
                if any((element.get(RESOURCE, "").startswith(MESH)
                        or element.get(ABOUT, "").startswith(MESH)
                        or element.get(RESOURCE) == base + "MedicalHeading") for element in expression.iter()):
                    raise ValueError("Existing logical links to MeSH prevent subject-categories integration; use mapping annotations.")
    baseline = {iri: (tuple(sorted(n.attrib.items())), tuple(signature(c) for c in n))
                for iri, n in original_classes.items()}
    raw_rows, pairs = read_mapping(mapping_path)
    reviews = read_reviews(review_csv, pairs)
    if mesh_hierarchy == "subject-categories" and any(r["decision"] == "subclass" for r in reviews.values()):
        raise ValueError("Reviewed ICD-to-MeSH subclass decisions cannot be combined with the subject-categories hierarchy. Use accept_link, or explicitly choose mesh_hierarchy=annotations for disease_concepts subclass reviews.")
    log("Reading all MeSH descriptors and validating every tree position")
    mesh = read_mesh(mesh_path, member=member, expected_year=str(mesh_release))
    descriptors, parents = mesh["descriptors"], mesh["parents"]
    mesh_targets = hierarchy_targets(descriptors, parents, base, mesh_hierarchy)
    declare_properties(root, base)
    container = ET.SubElement(root, CLASS, {ABOUT: base + "MedicalHeading"})
    literal(container, RDFS, "label", "Medical Heading", english=True)
    if mesh_hierarchy == "annotations":
        literal(container, RDFS, "comment", "Organizational superclass for MeSH subject categories in this EMPO representation. Native MeSH hierarchy is retained using meshBroader annotation links; it is not automatically a disease is-a hierarchy.")
    literal(ontology, base, "meshAnnotationRelease", mesh_release)
    literal(ontology, base, "meshSourceSHA256", input_hashes[str(mesh_path.resolve())])
    literal(ontology, base, "meshInputOWL_SHA256", input_hashes[str(owl_path.resolve())])
    source_hash = input_hashes[str(mapping_path.resolve())]
    literal(ontology, base, "meshMappingSourceSHA256", source_hash)
    literal(ontology, base, "meshMappingProvenanceFile", "icd_mesh_row_audit.csv")
    if review_csv:
        literal(ontology, base, "meshReviewSHA256", input_hashes[str(review_csv.resolve())])
    literal(ontology, RDFS, "comment", "Step 3: full MeSH descriptor hierarchy plus candidate/reviewed ICD mappings. Supplied mapping labels do not imply OWL equivalence or subsumption. No population memberships are inferred for MeSH headings. Missing supplementary records remain in the audit; no stub headings are invented.")
    broader_edges, descriptor_rows, descriptor_nodes = set(), [], []
    for identifier, d in sorted(descriptors.items()):
        node = ET.SubElement(root, CLASS, {ABOUT: MESH + identifier})
        descriptor_nodes.append(node)
        literal(node, RDFS, "label", d["label"], english=True)
        for target in sorted(mesh_targets[identifier]):
            link(node, RDFS, "subClassOf", target)
        literal(node, base, "nodeKind", "mesh_descriptor")
        literal(node, base, "meshDescriptorID", identifier)
        literal(node, base, "meshRelease", mesh_release)
        for position in d["tree_numbers"]:
            literal(node, base, "meshTreeNumber", position)
        for parent in sorted(parents[identifier]):
            link(node, base, "meshBroader", MESH + parent)
            broader_edges.add((MESH + identifier, MESH + parent))
        entry_terms = {t for c in d["concepts"] if c["preferred"] for t in c["terms"]} - {d["label"]}
        for term in sorted(entry_terms):
            literal(node, base, "meshEntryTerm", term, english=True)
        notes = {n["text"] for n in d["scope_notes"] if n["preferred"]}
        for note in sorted(notes):
            literal(node, base, "meshScopeNote", note, english=True)
        descriptor_rows.append({"mesh_id": identifier, "class_iri": MESH + identifier,
                                "label": d["label"], "tree_numbers": ";".join(d["tree_numbers"]),
                                "parent_ids": ";".join(sorted(parents[identifier])),
                                "preferred_concept_entry_terms": len(entry_terms),
                                "mesh_release": mesh_release})
    annotate_hierarchy(ontology, container, descriptor_nodes, base, mesh_hierarchy)
    log(f"Imported {len(descriptors):,} MeSH headings with {len(broader_edges):,} distinct broader-heading links")
    mapping_rows, by_pair, added_links, added_subclasses = [], {}, set(), set()
    code_status_counts = defaultdict(Counter)
    for (code, identifier), mapping in sorted(pairs.items()):
        issues = []
        if not valid_icd(code):
            issues.append("invalid_icd_code")
        elif code not in codes:
            issues.append("missing_icd_code")
        if not re.fullmatch(r"[DC][0-9]+", identifier):
            issues.append("invalid_mesh_id")
        elif identifier not in descriptors:
            issues.append("missing_supplementary_record" if identifier.startswith("C") else "missing_mesh_descriptor")
        review = reviews.get((code, identifier))
        if review and review["decision"] != "reject" and issues:
            raise ValueError(f"Cannot approve mapping with unavailable endpoints: {code}, {identifier}: {issues}.")
        status = issues[0] if issues else "candidate"
        if review:
            status = {"reject": "rejected", "accept_link": "accepted", "subclass": "approved_subclass"}[review["decision"]]
        icd_node = codes.get(code)
        iri = icd_node.get(ABOUT) if icd_node is not None else ""
        if status in {"candidate", "accepted", "approved_subclass"}:
            property_name = "candidateMeSH" if status == "candidate" else "Has_MeSH"
            link(icd_node, base, property_name, MESH + identifier)
            added_links.add((iri, f"{{{base}}}{property_name}", MESH + identifier))
            axiom(root, base, iri, base + property_name, MESH + identifier, status, mapping, review, source_hash)
            if status == "approved_subclass":
                link(icd_node, RDFS, "subClassOf", MESH + identifier)
                added_subclasses.add((iri, MESH + identifier))
                axiom(root, base, iri, RDFS + "subClassOf", MESH + identifier, status, mapping, review, source_hash)
        source_rows = mapping["source_rows"]
        out = {"icd_code": code, "mesh_id": identifier, "icd_class_iri": iri,
               "icd_label": icd_node.findtext(f"{{{base}}}icdPreferredName", "") or icd_node.findtext(f"{{{RDFS}}}label", "") if icd_node is not None else "",
               "mesh_label": descriptors.get(identifier, {}).get("label", ""),
               "mapping_types": json.dumps(sorted(mapping["mapping_types"]), ensure_ascii=False),
               "original_icd_codes": json.dumps(sorted(mapping["original_icd_codes"]), ensure_ascii=False),
               "source_levels": json.dumps(sorted(mapping["source_levels"])),
               "source_row_count": len(source_rows), "direct_original_rows": mapping["direct_original_rows"],
               "different_original_rows": len(source_rows) - mapping["direct_original_rows"],
               "status": status, "issues": ";".join(issues), "review_decision": review["decision"] if review else "",
               "reviewer": review["reviewer"] if review else "", "evidence": review["evidence"] if review else "",
               "class_interpretation": review["class_interpretation"] if review else ""}
        mapping_rows.append(out)
        by_pair[(code, identifier)] = out
        code_status_counts[code][status] += 1
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".mesh-stage-", dir=output_dir.parent))
    published = False
    try:
        log("Writing OWL, hierarchy, full mapping audit, and review template")
        for prefix, uri in (("rdf", RDF), ("rdfs", RDFS), ("owl", OWL), ("empo", base)):
            ET.register_namespace(prefix, uri)
        ET.indent(root, space="  ")
        tree.write(stage / OUTPUTS[0], encoding="utf-8", xml_declaration=True)
        write_csv(stage / "mesh_descriptors.csv", tuple(descriptor_rows[0]), descriptor_rows)
        hierarchy_fields = ("child_id", "parent_id", "child_tree", "parent_tree")
        hierarchy_rows = list(mesh["tree_parents"])
        position_ids = {t: k for k, d in descriptors.items() for t in d["tree_numbers"]}
        hierarchy_rows.extend({"child_id": position_ids[t], "parent_id": "", "child_tree": t, "parent_tree": ""}
                              for t in mesh["root_trees"])
        write_csv(stage / "mesh_hierarchy.csv", hierarchy_fields, hierarchy_rows)
        with (stage / "mesh_concepts.jsonl").open("w", encoding="utf-8") as stream:
            for identifier, d in sorted(descriptors.items()):
                stream.write(json.dumps(d, ensure_ascii=False, sort_keys=True) + "\n")
        write_csv(stage / "icd_mesh_mappings.csv", MAPPING_FIELDS, mapping_rows)
        row_fields = ("source_row", "ICD", "level", "value", "mesh", "mapping_type", "icd_code", "mesh_id", "status", "issues", "link_basis")
        row_counts = Counter()
        def audit_rows():
            for original in raw_rows:
                match = by_pair[(original["icd_code"], original["mesh_id"])]
                row_counts[match["status"]] += 1
                yield dict(original, status=match["status"], issues=match["issues"],
                           link_basis="direct_value_code" if norm_icd(original["ICD"]) == original["icd_code"] else "value_differs_from_original_no_propagation")
        write_csv(stage / "icd_mesh_row_audit.csv", row_fields, audit_rows())
        coverage_fields = ("class_iri", "icd_code", "node_kind", "candidate_count", "accepted_count", "approved_subclass_count", "rejected_count", "other_unresolved_count", "coverage_status")
        def coverage_rows():
            for iri, code, kind in sorted(all_icd):
                counts = code_status_counts[code] if kind == "code" else Counter()
                candidate, accepted, subclasses, rejected = (counts[k] for k in ("candidate", "accepted", "approved_subclass", "rejected"))
                yield {"class_iri": iri, "icd_code": code, "node_kind": kind,
                       "candidate_count": candidate, "accepted_count": accepted, "approved_subclass_count": subclasses,
                       "rejected_count": rejected, "other_unresolved_count": sum(counts.values())-candidate-accepted-subclasses-rejected,
                       "coverage_status": "reviewed_mapping" if accepted+subclasses else "candidate_only" if candidate else "unresolved_or_rejected" if counts else "no_mapping_in_supplied_table"}
        write_csv(stage / "icd_mesh_coverage.csv", coverage_fields, coverage_rows())
        template_fields = REVIEW_FIELDS + ("icd_label", "mesh_label", "original_mapping_types", "current_status", "endpoint_issues", "mesh_scope_notes")
        def template_rows():
            for row in mapping_rows:
                review = reviews.get((row["icd_code"], row["mesh_id"]), {})
                yield {"icd_code": row["icd_code"], "mesh_id": row["mesh_id"],
                       **{k: review.get(k, "") for k in REVIEW_FIELDS[2:]},
                       "icd_label": row["icd_label"], "mesh_label": row["mesh_label"],
                       "original_mapping_types": row["mapping_types"], "current_status": row["status"], "endpoint_issues": row["issues"],
                       "mesh_scope_notes": json.dumps(descriptors.get(row["mesh_id"], {}).get("scope_notes", []), ensure_ascii=False)}
        write_csv(stage / "mapping_review_template.csv", template_fields, template_rows())
        log("Verifying unchanged input classes and every serialized MeSH/mapping edge")
        validate_serialized(stage / OUTPUTS[0], baseline, base, set(descriptors), broader_edges, added_links, added_subclasses,
                            expected_mesh_subclasses=mesh_targets, mesh_hierarchy=mesh_hierarchy)
        # Validate exact audit cardinality and status per mapping, not only XML parse success.
        with (stage / "icd_mesh_mappings.csv").open(encoding="utf-8", newline="") as stream:
            actual = [(r["icd_code"], r["mesh_id"], r["status"]) for r in csv.DictReader(stream)]
        if actual != [(r["icd_code"], r["mesh_id"], r["status"]) for r in mapping_rows]:
            raise ValueError("Incomplete mapping table export.")
        expected_rows = list(range(2, len(raw_rows) + 2))
        with (stage / "icd_mesh_row_audit.csv").open(encoding="utf-8", newline="") as stream:
            if [int(r["source_row"]) for r in csv.DictReader(stream)] != expected_rows:
                raise ValueError("Incomplete raw mapping audit export.")
        for p in inputs:
            if sha256(p) != input_hashes[str(p.resolve())]:
                raise ValueError(f"Input file changed during processing: {p.name}.")
        status_counts = Counter(r["status"] for r in mapping_rows)
        issue_counts = Counter(issue for r in mapping_rows for issue in r["issues"].split(";") if issue)
        report = {
            "mesh_release": str(mesh_release), "mesh_source": mesh["provenance"],
            "mesh_hierarchy_mode": mesh_hierarchy,
            "mesh_subclass_edges": sum(target.startswith(MESH) for targets in mesh_targets.values() for target in targets),
            "medical_heading_subclass_edges": sum(base + "MedicalHeading" in targets for targets in mesh_targets.values()),
            "input_sha256": input_hashes, "mapping_source_provenance": "Supplied CSV; generation method and terminology release are unknown.",
            "source_mapping_types": dict(Counter(r["mapping_type"] for r in raw_rows)),
            "original_classes": len(original_classes), "mesh_descriptors_added": len(descriptors),
            "total_output_classes": len(original_classes) + len(descriptors) + 1,
            "mesh_broader_edges": len(broader_edges), "mesh_tree_positions": len(position_ids),
            "mesh_hierarchy_rows": len(hierarchy_rows), "mesh_parser_counts": mesh["counts"],
            "mapping_rows": len(raw_rows), "distinct_value_mesh_pairs": len(pairs),
            "mapping_pair_status_counts": dict(status_counts), "mapping_row_status_counts": dict(row_counts),
            "mapping_pair_issue_counts": dict(issue_counts), "review_decisions": len(reviews),
            "candidate_links": status_counts["candidate"], "accepted_links": status_counts["accepted"] + status_counts["approved_subclass"],
            "approved_subclass_links": len(added_subclasses),
            "distinct_icd_codes_with_candidate_links": len({r["icd_code"] for r in mapping_rows if r["status"] == "candidate"}),
            "missing_mesh_ids": sorted({m for _, m in pairs if m not in descriptors}),
            "missing_icd_codes": sorted({c for c, _ in pairs if c not in codes}),
            "coverage_nodes": len(all_icd),
            "policies": {
                "scope": "All descriptors in supplied XML, including headings without ICD mappings.",
                "hierarchy": SUBJECT_COMMENT if mesh_hierarchy == "subject-categories" else "Descriptor classes directly under MedicalHeading; native descriptor hierarchy in meshBroader annotations and tree-position CSV. Not automatically MeSH disease subclass assertions.",
                "mappings": "Use exact normalized CSV value, not original ICD. No seven-character truncation, prefix matching, parent propagation, or mapping-type promotion.",
                "review": "Explicit accept_link required for Has_MeSH in subject-categories mode. Subclass reviews require annotations mode, reviewer, evidence and disease_concepts interpretation; the program does not establish clinical validity.",
                "missing_sources": "Supplementary concepts are reported only. No unverified placeholder headings or new ICD nodes.",
                "terms": "OWL entry terms and scope notes use the preferred MeSH concept only. All other concept terms/relationships are retained separately in mesh_concepts.jsonl.",
                "population": "Original memberships preserved; no MeSH population memberships inferred.",
            },
            "validation": {"xml_parses": True, "original_class_content_preserved": True,
                           "mesh_hierarchy_exact": True, "mapping_links_exact": True,
                           "all_mapping_rows_audited": True, "input_hashes_unchanged": True,
                           "owl_reasoner_run": False, "source_mapping_semantics_validated": False},
            "output_sha256": {name: sha256(stage / name) for name in OUTPUTS[:-1]},
        }
        (stage / OUTPUTS[-1]).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if output_dir.exists():
            raise ValueError("Output directory appeared during processing; refusing to replace it.")
        stage.rename(output_dir)
        published = True
        log(f"Finished: {status_counts['candidate']:,} candidate links; {report['accepted_links']:,} accepted links; {len(added_subclasses):,} reviewed subclass links")
        return report
    finally:
        if not published:
            shutil.rmtree(stage, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owl", type=Path, required=True, help="Original ICD/UMLS OWL before MeSH integration")
    parser.add_argument("--mesh-xml", type=Path, required=True, help="MeSH descriptor XML, ZIP, or gzip")
    parser.add_argument("--mapping-csv", type=Path, required=True, help="CSV with ICD,level,value,mesh,mapping_type")
    parser.add_argument("--output-dir", type=Path, default=Path("output_mesh"), help="New directory; must not already exist")
    parser.add_argument("--mesh-release", default="2025")
    parser.add_argument("--mesh-member", help="Exact descriptor XML member when ZIP is ambiguous")
    parser.add_argument("--review-csv", type=Path, help="Explicit reject/accept_link/subclass decisions with reviewer and evidence")
    parser.add_argument("--mesh-hierarchy", choices=("subject-categories", "annotations"), default="subject-categories",
                        help="Visible subject-category class hierarchy (default) or legacy annotation-only hierarchy")
    args = parser.parse_args()
    try:
        report = integrate(args.owl, args.mesh_xml, args.mapping_csv, args.output_dir,
                           mesh_release=args.mesh_release, member=args.mesh_member, review_csv=args.review_csv,
                           mesh_hierarchy=args.mesh_hierarchy,
                           progress=lambda text: print(text, file=sys.stderr, flush=True))
    except (OSError, ValueError, ET.ParseError) as exc:
        parser.exit(2, f"MeSH integration failed: {exc}\n")
    print(json.dumps(report["mapping_pair_status_counts"], indent=2))
    print(f"Written: {args.output_dir / OUTPUTS[0]}")


if __name__ == "__main__":
    main()
