#!/usr/bin/env python3
"""Build the first EMPO ICD/population backbone from a MarketScan code list."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import tempfile
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

from icd_reference import parse_references

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"
XSD = "http://www.w3.org/2001/XMLSchema#"
DEFAULT_BASE = "https://example.org/empo/"
POPULATIONS = {
    "Maternal": ("Maternal", ""),
    "0": ("Pediatric", "0"),
    "1-12": ("Pediatric", "1-12"),
    "13-17": ("Pediatric", "13-17"),
}
AGE_TERMS = {"0": "PediatricAge0", "1-12": "PediatricAge1_12", "13-17": "PediatricAge13_17"}


def normalize_code(value: str) -> str:
    value = value.strip().upper()
    if "." in value and not re.fullmatch(r"[A-Z][0-9][A-Z0-9]\.[A-Z0-9]{1,4}", value):
        raise ValueError(f"Unexpected ICD code format: {value!r}")
    value = value.replace(".", "")
    if not re.fullmatch(r"[A-Z][0-9][A-Z0-9]{1,5}", value):
        raise ValueError(f"Unexpected ICD-10-shaped code: {value!r}")
    return value


def group_code(code: str) -> str:
    """User's analytical rule. X placeholders are intentionally retained."""
    return code[:6] if len(code) == 7 else code


def dotted(code: str) -> str:
    return code[:3] + "." + code[3:] if len(code) > 3 else code


def write_json(path, value):
    """Publish a complete JSON file in one rename, never a partial export."""
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    temporary.replace(path)


def read_observations(path: Path):
    observations = set()
    raw_rows = excluded = duplicates = 0
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not {"ICD10", "population"} <= set(reader.fieldnames):
            raise ValueError("Input must have ICD10 and population columns.")
        for line, row in enumerate(reader, 2):
            raw_rows += 1
            raw = row.get("ICD10")
            population = row.get("population")
            if raw is None or population is None:
                raise ValueError(f"Incomplete CSV row {line}")
            if raw.strip().casefold() == "nodx":
                excluded += 1
                continue
            population = population.strip()
            if population not in POPULATIONS:
                raise ValueError(f"Unknown population {population!r} on row {line}")
            try:
                code = normalize_code(raw)
            except ValueError as exc:
                raise ValueError(f"Row {line}: {exc}") from exc
            pair = (code, population)
            duplicates += pair in observations
            observations.add(pair)
    if not observations:
        raise ValueError("No usable ICD/population observations in input.")
    return observations, {
        "input_rows": raw_rows,
        "excluded_NoDx_rows": excluded,
        "duplicate_rows_removed_after_NoDx": duplicates,
        "unique_original_code_population_pairs": len(observations),
    }


def check_dag(nodes):
    """Check both parent existence and cycles, including disconnected nodes."""
    state = {}

    def visit(key):
        if state.get(key) == 1:
            raise ValueError(f"Subclass cycle at {key}")
        if state.get(key) == 2:
            return
        if key not in nodes:
            raise ValueError(f"Missing parent node: {key}")
        state[key] = 1
        for parent in nodes[key]["parents"]:
            visit(parent)
        state[key] = 2

    for key in nodes:
        visit(key)


def build_model(observations, references):
    check_dag(references)

    @lru_cache(None)
    def ancestors(key):
        found = {key}
        for parent in references[key]["parents"]:
            found.update(ancestors(parent))
        return frozenset(found)

    members = defaultdict(set)
    memberships = defaultdict(set)
    for code, source_population in observations:
        grouped = group_code(code)
        members[grouped].add(code)
        memberships[grouped].add(source_population)

    active = set()
    groups = {}
    synthetic = {}
    unresolved_originals = sorted({code for code, _ in observations if "code:" + code not in references})

    def retain(key):
        if key in active:
            return
        active.add(key)
        for parent in references[key]["parents"]:
            retain(parent)

    for grouped in sorted(members):
        codes = members[grouped]
        known_keys = ["code:" + code for code in sorted(codes) if "code:" + code in references]
        unknown = sorted(code for code in codes if "code:" + code not in references)
        candidate = "code:" + grouped
        official = candidate in references and references[candidate].get("materialize", True)
        compatible = official and bool(known_keys) and not unknown and all(candidate in ancestors(key) for key in known_keys)
        if compatible:
            key = candidate
            kind = "official"
            retain(key)
        else:
            key = "group:" + grouped
            kind = "analytical_group"
            common = set.intersection(*(set(ancestors(k)) for k in known_keys)) if known_keys else set()
            common = {k for k in common if references[k].get("materialize", True)}
            # Keep only most specific common ancestors, never arbitrary prefixes.
            parents = sorted(k for k in common if not any(k != other and k in ancestors(other) for other in common))
            if not parents:
                kind = "unresolved_group"
            for parent in parents:
                retain(parent)
            parent_label = references[parents[0]]["label"] if len(parents) == 1 else "ICD code group"
            synthetic[key] = {
                "label": f"{parent_label} [group {dotted(grouped)}]",
                "kind": kind, "code": dotted(grouped), "parents": parents,
                "source_releases": [], "selected_release": "",
            }
        groups[grouped] = {
            "key": key, "kind": kind, "members": sorted(codes),
            "memberships": sorted(memberships[grouped]),
            "unresolved_members": unknown,
        }
    nodes = {key: references[key] for key in sorted(active)}
    nodes.update(synthetic)
    check_dag(nodes)
    return nodes, groups, unresolved_originals


def iri_for(key, base):
    kind, code = key.split(":", 1)
    prefix = {"code": "ICD_", "group": "ICD_Group_", "chapter": "ICD_Chapter_", "block": "ICD_Block_"}[kind]
    return base + prefix + re.sub(r"[^A-Za-z0-9_]", "_", code)


def write_owl(path, base, nodes, groups, source_name, source_hash, reference_hash):
    for prefix, namespace in [("rdf", RDF), ("rdfs", RDFS), ("owl", OWL), ("empo", base)]:
        ET.register_namespace(prefix, namespace)
    root = ET.Element(f"{{{RDF}}}RDF")

    def literal(element, namespace, name, value):
        child = ET.SubElement(element, f"{{{namespace}}}{name}")
        child.text = str(value)
        return child

    def link(element, namespace, name, value):
        return ET.SubElement(element, f"{{{namespace}}}{name}", {f"{{{RDF}}}resource": value})

    ontology = ET.SubElement(root, f"{{{OWL}}}Ontology", {f"{{{RDF}}}about": base.rstrip("/#")})
    literal(ontology, RDFS, "label", "EMPO ICD and population backbone — step 1")
    literal(ontology, RDFS, "comment", "Fresh build for MarketScan calendar years 2016–2021. Official ICD hierarchy plus analytical seven-to-six-character groups. Population annotations record observed cohort membership, not exclusivity or disease-specificity. Reference validity means found in at least one included release; claim dates are unavailable.")
    literal(ontology, base, "sourceFile", source_name)
    literal(ontology, base, "sourceSHA256", source_hash)
    literal(ontology, base, "referenceManifestSHA256", reference_hash)
    literal(ontology, base, "groupingRule", "Remove only character 7 from seven-character undotted codes; retain X placeholders and all original code–population associations.")
    properties = {
        "ICD_Code": "ICD code or analytical group code (undotted)",
        "originalICDCode": "Original undotted code contributing to this observed group",
        "observedInPopulation": "At least one constituent code was observed in this source cohort",
        "sourcePopulationLabel": "Original population label from the CSV",
        "referenceRelease": "Release selected for the official label and parent relationships",
        "availableInRelease": "Reference release containing this node",
        "nodeKind": "Official hierarchy node, analytical group, or unresolved group",
        "directlyObserved": "True only when this node represents a group observed in the input",
        "unresolvedOriginalCode": "Input code not found in any included reference release",
        "sourceFile": "Source file name", "sourceSHA256": "SHA256 of source input",
        "referenceManifestSHA256": "SHA256 of the reference source manifest",
        "groupingRule": "Analytical grouping rule",
    }
    for name, description in properties.items():
        prop = ET.SubElement(root, f"{{{OWL}}}AnnotationProperty", {f"{{{RDF}}}about": base + name})
        literal(prop, RDFS, "label", name)
        literal(prop, RDFS, "comment", description)

    def new_class(local, label, parent=None, comment=None):
        element = ET.SubElement(root, f"{{{OWL}}}Class", {f"{{{RDF}}}about": base + local})
        literal(element, RDFS, "label", label)
        if parent:
            link(element, RDFS, "subClassOf", base + parent)
        if comment:
            literal(element, RDFS, "comment", comment)
        return element

    new_class("Observation", "ICD-coded observation", comment="Includes ICD conditions, injuries, symptoms, encounters and other coded observations.")
    new_class("SpecialPopulation", "Population")
    new_class("Maternal", "Maternal", "SpecialPopulation", "Maternal cohort supplied by the researcher; detailed pregnancy eligibility/window remains to be documented.")
    new_class("Pediatric", "Pediatric", "SpecialPopulation")
    for age, term in AGE_TERMS.items():
        new_class(term, f"Pediatric age group {age}", "Pediatric", "Age label preserved from the source CSV. Age 0 is not further redefined in this build.")
    if any(g["kind"] == "unresolved_group" for g in groups.values()):
        new_class("UnresolvedICDGroup", "ICD groups requiring reference review", "Observation")

    observed_by_key = {group["key"]: (code, group) for code, group in groups.items()}
    for key in sorted(nodes):
        node = nodes[key]
        element = ET.SubElement(root, f"{{{OWL}}}Class", {f"{{{RDF}}}about": iri_for(key, base)})
        literal(element, RDFS, "label", node["label"])
        literal(element, base, "nodeKind", node["kind"])
        if key.startswith(("code:", "group:")):
            literal(element, base, "ICD_Code", key.split(":", 1)[1])
        if node["parents"]:
            for parent in node["parents"]:
                link(element, RDFS, "subClassOf", iri_for(parent, base))
        else:
            parent = "UnresolvedICDGroup" if node["kind"] == "unresolved_group" else "Observation"
            link(element, RDFS, "subClassOf", base + parent)
        if node.get("selected_release"):
            literal(element, base, "referenceRelease", node["selected_release"])
        for release in sorted(node.get("source_releases", [])):
            literal(element, base, "availableInRelease", release)
        flag = literal(element, base, "directlyObserved", "true" if key in observed_by_key else "false")
        flag.set(f"{{{RDF}}}datatype", XSD + "boolean")
        if key not in observed_by_key:
            continue
        _, group = observed_by_key[key]
        for original in group["members"]:
            literal(element, base, "originalICDCode", original)
        target_populations = set()
        for source_population in group["memberships"]:
            population, age = POPULATIONS[source_population]
            target_populations.add(population)
            if age:
                target_populations.add(AGE_TERMS[age])
            literal(element, base, "sourcePopulationLabel", source_population)
        for target in sorted(target_populations):
            link(element, base, "observedInPopulation", base + target)
        for original in group["unresolved_members"]:
            literal(element, base, "unresolvedOriginalCode", original)
        if group["kind"] != "official":
            literal(element, RDFS, "comment", "Analytical group created by seven-to-six truncation, not asserted to be an official billable code. Parent is a common official ancestor of reference-matched constituent codes; any unmatched members are listed separately.")
    ET.indent(root, space="  ")
    with tempfile.NamedTemporaryFile(dir=Path(path).parent, suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        ET.ElementTree(root).write(stream, encoding="utf-8", xml_declaration=True)
    temporary.replace(path)


def build(input_path, manifest_path, output_dir, base=DEFAULT_BASE):
    input_path, manifest_path, output_dir = map(Path, (input_path, manifest_path, output_dir))
    if urlparse(base).scheme not in {"http", "https"} or not base.endswith(("/", "#")):
        raise ValueError("--base-iri must be an absolute HTTP(S) IRI ending in / or #")
    observations, report = read_observations(input_path)
    references = parse_references(manifest_path)
    nodes, groups, unresolved = build_model(observations, references)
    output_dir.mkdir(parents=True, exist_ok=True)
    source_hash = hashlib.sha256(input_path.read_bytes()).hexdigest()
    manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    owl_path = output_dir / "empo_icd_backbone.owl"
    write_owl(owl_path, base, nodes, groups, input_path.name, source_hash, manifest_hash)
    mapping_path = output_dir / "code_population_mapping.csv"
    with tempfile.NamedTemporaryFile(mode="w", dir=output_dir, encoding="utf-8", newline="", suffix=".tmp", delete=False) as stream:
        temporary_mapping = Path(stream.name)
        writer = csv.writer(stream)
        writer.writerow(["original_icd", "grouped_icd", "class_iri", "population", "age_group", "source_population", "reference_status", "group_kind"])
        for original, source_population in sorted(observations):
            grouped = group_code(original)
            group = groups[grouped]
            population, age = POPULATIONS[source_population]
            status = "found_in_reference_period" if "code:" + original in references else "not_found_in_reference_period"
            writer.writerow([original, grouped, iri_for(group["key"], base), population, age, source_population, status, group["kind"]])
    with temporary_mapping.open(encoding="utf-8", newline="") as stream:
        exported = [(row["original_icd"], row["source_population"]) for row in csv.DictReader(stream)]
    if len(exported) != len(observations) or set(exported) != observations:
        raise ValueError("Mapping export did not preserve every original code–population association")
    temporary_mapping.replace(mapping_path)
    conflicts = {key: node.get("conflicts") for key, node in nodes.items() if node.get("conflicts")}
    provenance = {key: node.get("release_provenance", []) for key, node in nodes.items() if node.get("release_provenance")}
    write_json(output_dir / "reference_conflicts.json", conflicts)
    write_json(output_dir / "reference_provenance.json", provenance)
    maternal = {g for g, d in groups.items() if "Maternal" in d["memberships"]}
    pediatric = {g for g, d in groups.items() if any(p != "Maternal" for p in d["memberships"])}
    report.update({
        "input_file": input_path.name, "input_sha256": source_hash,
        "reference_manifest_sha256": manifest_hash, "base_iri": base,
        "study_calendar_years": [2016, 2021],
        "grouping": "seven_to_six_keep_X",
        "unique_original_codes": len({code for code, _ in observations}),
        "observed_code_groups": len(groups),
        "observed_group_kinds": dict(sorted(Counter(g["kind"] for g in groups.values()).items())),
        "hierarchy_node_kinds": dict(sorted(Counter(n["kind"] for n in nodes.values()).items())),
        "icd_hierarchy_nodes": len(nodes),
        "added_ancestor_nodes": len(nodes) - len(groups),
        "maternal_groups": len(maternal), "pediatric_groups": len(pediatric),
        "groups_in_both_populations": len(maternal & pediatric),
        "age_group_counts": {age: sum(age in g["memberships"] for g in groups.values()) for age in AGE_TERMS},
        "unresolved_original_codes": unresolved,
        "groups_with_unresolved_members": sum(bool(g["unresolved_members"]) for g in groups.values()),
        "reference_nodes_with_historical_changes": len(conflicts),
        "mapping_sha256": hashlib.sha256(mapping_path.read_bytes()).hexdigest(),
        "validation": {"hierarchy_acyclic": True, "all_parents_present": True, "all_original_population_pairs_preserved": True, "official_release_validation": "present in at least one supplied applicable release; not claim-date validation", "reasoner_run": False},
    })
    # XML parse and class identity check on the actual serialized deliverable.
    parsed = ET.parse(owl_path).getroot()
    classes = parsed.findall(f"{{{OWL}}}Class")
    iris = [node.attrib[f"{{{RDF}}}about"] for node in classes]
    if len(iris) != len(set(iris)):
        raise ValueError("Duplicate class IRIs in serialized OWL")
    report["owl_class_count"] = len(classes)
    report["owl_sha256"] = hashlib.sha256(owl_path.read_bytes()).hexdigest()
    report["validation"]["rdf_xml_well_formed"] = True
    write_json(output_dir / "build_report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="CSV with ICD10 and population columns")
    parser.add_argument("--references", type=Path, default=Path(__file__).parent / "references" / "sources.json")
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--base-iri", default=DEFAULT_BASE)
    args = parser.parse_args()
    try:
        result = build(args.input, args.references, args.output_dir, args.base_iri)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Build failed: {exc}\n")
    print(json.dumps({k: result[k] for k in ["unique_original_codes", "observed_code_groups", "icd_hierarchy_nodes", "owl_class_count", "groups_with_unresolved_members"]}, indent=2))
    print(f"Written: {args.output_dir / 'empo_icd_backbone.owl'}")


if __name__ == "__main__":
    main()
