#!/usr/bin/env python3
"""Make the native MeSH tree visible as a documented subject-category class hierarchy.

Patch an existing EMPO Step 3 RDF/XML file after checking it against the original
MeSH XML/ZIP. Inputs are read-only. A fresh output directory is published atomically.
Python 3.10+, standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET

from integrate_mesh import (
    ABOUT, RESOURCE, CLASS, SUBCLASS, RDF, RDFS, OWL, MESH,
    SUBJECT_COMMENT, annotate_hierarchy, declare_properties, hierarchy_targets,
    literal, link, sha256, signature,
)
from mesh_reader import read_mesh

OWL_NAME = "empo_icd_umls_mesh.owl"
REPORT_NAME = "mesh_hierarchy_fix_report.json"


def _digest(node):
    return hashlib.sha256(repr(signature(node)).encode("utf-8")).hexdigest()


def _inspect(root, base, descriptors, parents, *, projected=False, expected_targets=None):
    """Check exact named descriptor identities, native hierarchy and tree positions."""
    classes = root.findall(CLASS)
    by_iri = {node.get(ABOUT): node for node in classes}
    if None in by_iri or len(classes) != len(by_iri):
        raise ValueError("Input contains unnamed or duplicate OWL classes.")
    # The correction supports the explicit, top-level EMPO builder representation.
    if len(list(root.iter(CLASS))) != len(classes):
        raise ValueError("Nested class declarations are not supported by this correction.")
    descriptor_nodes = {}
    for iri, node in by_iri.items():
        identifiers = node.findall(f"{{{base}}}meshDescriptorID")
        is_mesh = iri.startswith(MESH)
        if not is_mesh and identifiers:
            raise ValueError("MeSH identifier on a noncanonical class IRI.")
        if not is_mesh:
            continue
        identifier = iri[len(MESH):]
        if not re.fullmatch(r"D[0-9]+", identifier) or identifier not in descriptors:
            raise ValueError(f"Unknown MeSH descriptor class {iri}.")
        if len(identifiers) != 1 or identifiers[0].text != identifier:
            raise ValueError(f"Missing or inconsistent MeSH descriptor ID for {iri}.")
        if node.findtext(f"{{{base}}}nodeKind") != "mesh_descriptor":
            raise ValueError(f"Incorrect nodeKind for {iri}.")
        descriptor_nodes[identifier] = node
        positions = [(child.text or "") for child in node.findall(f"{{{base}}}meshTreeNumber")]
        if len(positions) != len(set(positions)) or set(positions) != set(descriptors[identifier]["tree_numbers"]):
            raise ValueError(f"MeSH tree positions differ from source for {identifier}.")
        broader = [child.get(RESOURCE) for child in node.findall(f"{{{base}}}meshBroader")]
        expected = {MESH + parent for parent in parents[identifier]}
        if len(broader) != len(set(broader)) or set(broader) != expected:
            raise ValueError(f"MeSH broader parents differ from source for {identifier}; missing parents or cycles are not allowed.")
        subclasses = node.findall(SUBCLASS)
        if any(len(child) or not child.get(RESOURCE) for child in subclasses):
            raise ValueError("Complex or anonymous MeSH subclass expressions are not supported.")
        targets = [child.get(RESOURCE) for child in subclasses]
        if len(targets) != len(set(targets)):
            raise ValueError(f"Duplicate MeSH subclass edges for {identifier}.")
        allowed = expected | {base + "MedicalHeading"}
        if not set(targets) <= allowed:
            raise ValueError("Existing cross-vocabulary or nonnative MeSH subclass link prevents subject-category projection.")
        if projected:
            if set(targets) != expected_targets[identifier]:
                raise ValueError("Serialized MeSH subclass hierarchy differs from planned projection.")
            interpretations = node.findall(f"{{{base}}}meshClassInterpretation")
            if len(interpretations) != 1 or interpretations[0].text != "subject_categories":
                raise ValueError("Serialized MeSH subject-category interpretation is missing or ambiguous.")
    if set(descriptor_nodes) != set(descriptors):
        raise ValueError("MeSH descriptor IDs differ from the supplied XML source.")
    if base + "MedicalHeading" not in by_iri:
        raise ValueError("Input has no MedicalHeading class.")
    # Logical links that cross from the ICD/disease model into subject headings
    # cannot be reinterpreted silently. Mapping annotations are unaffected.
    for iri, node in by_iri.items():
        for expression in node.findall(SUBCLASS) + node.findall(f"{{{OWL}}}equivalentClass"):
            references = {e.get(RESOURCE) for e in expression.iter() if e.get(RESOURCE)}
            references.update(e.get(ABOUT) for e in expression.iter() if e.get(ABOUT))
            mesh_refs = {ref for ref in references if ref.startswith(MESH)}
            if expression.tag == f"{{{OWL}}}equivalentClass" and (iri.startswith(MESH) or mesh_refs):
                raise ValueError("Existing MeSH equivalentClass axiom prevents subject-category projection.")
            if not iri.startswith(MESH) and mesh_refs:
                raise ValueError("Existing cross-vocabulary subclass link to MeSH prevents subject-category projection.")
    return by_iri, descriptor_nodes


def _preservation_digests(root, base, descriptor_iris, *, original_count):
    """Fingerprint original content excluding precisely the authorized edit fields."""
    answer = []
    for node in list(root)[:original_count]:
        iri = node.get(ABOUT)
        children = []
        for child in node:
            if iri in descriptor_iris and child.tag == SUBCLASS:
                continue
            if (node.tag == f"{{{OWL}}}Ontology" or iri in descriptor_iris or iri == base + "MedicalHeading"):
                if child.tag in {f"{{{base}}}meshHierarchyMode", f"{{{base}}}meshClassInterpretation", f"{{{base}}}meshHierarchyFixInputSHA256"}:
                    continue
                if child.tag == f"{{{RDFS}}}comment" and child.text == SUBJECT_COMMENT:
                    continue
            children.append(signature(child))
        value = (node.tag, tuple(sorted(node.attrib.items())), (node.text or "").strip(), tuple(children))
        answer.append(hashlib.sha256(repr(value).encode("utf-8")).hexdigest())
    return answer


def _serialized_digests(path):
    """Stream exact top-level content validation without retaining a second graph."""
    depth, digests, root_attributes = 0, [], None
    for event, node in ET.iterparse(path, events=("start", "end")):
        if event == "start":
            depth += 1
            if depth == 1:
                if node.tag != f"{{{RDF}}}RDF":
                    raise ValueError("Serialized output is not RDF/XML.")
                root_attributes = dict(node.attrib)
        else:
            if depth == 2:
                digests.append(_digest(node))
                node.clear()
            depth -= 1
    return digests, root_attributes


def fix_hierarchy(owl_path, mesh_path, output_dir, *, mesh_release="2025", member=None, progress=None):
    """Correct one existing Step 3 file; returns a report after atomic publication."""
    owl_path, mesh_path, output_dir = map(Path, (owl_path, mesh_path, output_dir))
    if output_dir.exists():
        raise ValueError("Output directory already exists. Use a fresh output directory.")
    inputs = (owl_path, mesh_path)
    hashes = {str(path.resolve()): sha256(path) for path in inputs}
    log = progress or (lambda message: None)
    log("Reading the Step 3 ontology and verifying the original MeSH source")
    tree = ET.parse(owl_path)
    root = tree.getroot()
    if root.tag != f"{{{RDF}}}RDF":
        raise ValueError("Expected EMPO RDF/XML.")
    bases = {child.tag[1:].split("}", 1)[0] for node in root.findall(CLASS) for child in node
             if child.tag.endswith("}ICD_Code")}
    if len(bases) != 1:
        raise ValueError("Cannot identify a unique ICD annotation namespace.")
    base = bases.pop()
    ontologies = root.findall(f"{{{OWL}}}Ontology")
    if len(ontologies) != 1:
        raise ValueError("Expected exactly one owl:Ontology declaration.")
    ontology = ontologies[0]
    modes = ontology.findall(f"{{{base}}}meshHierarchyMode")
    if any((node.text or "") == "subject-categories" for node in modes):
        raise ValueError("Input already has the subject-categories projection.")
    if modes and (len(modes) != 1 or modes[0].text != "annotations"):
        raise ValueError("Unknown or ambiguous existing MeSH hierarchy mode.")
    releases = ontology.findall(f"{{{base}}}meshAnnotationRelease")
    if len(releases) != 1 or releases[0].text != str(mesh_release):
        raise ValueError("MeSH release does not match the existing Step 3 ontology.")
    sources = ontology.findall(f"{{{base}}}meshSourceSHA256")
    if len(sources) != 1 or sources[0].text != hashes[str(mesh_path.resolve())]:
        raise ValueError("MeSH source SHA256 does not match the source recorded in the ontology.")
    mesh = read_mesh(mesh_path, member=member, expected_year=str(mesh_release))
    descriptors, parents = mesh["descriptors"], mesh["parents"]
    by_iri, descriptor_nodes = _inspect(root, base, descriptors, parents)
    container = by_iri[base + "MedicalHeading"]
    for node in (ontology, container, *descriptor_nodes.values()):
        if node.findall(f"{{{base}}}meshClassInterpretation"):
            raise ValueError("Existing class interpretation prevents a silent change to subject categories.")
        if node.findall(f"{{{base}}}meshHierarchyFixInputSHA256"):
            raise ValueError("Input has already undergone a hierarchy correction.")
    descriptor_iris = {MESH + identifier for identifier in descriptors}
    original_count = len(root)
    original_attrs = dict(root.attrib)
    before = _preservation_digests(root, base, descriptor_iris, original_count=original_count)
    targets = hierarchy_targets(descriptors, parents, base)
    old_flat_edges = sum(child.get(RESOURCE) == base + "MedicalHeading"
                         for node in descriptor_nodes.values() for child in node.findall(SUBCLASS))
    flat_edges_removed = sum(base + "MedicalHeading" not in targets[identifier]
                             and any(child.get(RESOURCE) == base + "MedicalHeading" for child in node.findall(SUBCLASS))
                             for identifier, node in descriptor_nodes.items())
    old_hierarchy_edges = sum(len(node.findall(SUBCLASS)) for node in descriptor_nodes.values())
    log("Projecting immediate MeSH parents into the visible subject-category class hierarchy")
    for identifier, node in descriptor_nodes.items():
        for child in list(node.findall(SUBCLASS)):
            node.remove(child)
        for target in sorted(targets[identifier]):
            link(node, RDFS, "subClassOf", target)
    # annotations is an explicit legacy mode, so replace only that mode metadata.
    for mode in modes:
        ontology.remove(mode)
    declare_properties(root, base)
    annotate_hierarchy(ontology, container, list(descriptor_nodes.values()), base, "subject-categories")
    literal(ontology, base, "meshHierarchyFixInputSHA256", hashes[str(owl_path.resolve())])
    if before != _preservation_digests(root, base, descriptor_iris, original_count=original_count) or dict(root.attrib) != original_attrs:
        raise ValueError("Original content outside the permitted hierarchy/metadata changes was modified.")
    _inspect(root, base, descriptors, parents, projected=True, expected_targets=targets)
    expected = [_digest(node) for node in root]
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".mesh-hierarchy-stage-", dir=output_dir.parent))
    published = False
    try:
        for prefix, uri in (("rdf", RDF), ("rdfs", RDFS), ("owl", OWL), ("empo", base)):
            ET.register_namespace(prefix, uri)
        ET.indent(root, space="  ")
        tree.write(stage / OWL_NAME, encoding="utf-8", xml_declaration=True)
        log("Verifying every serialized element and the unchanged source files")
        actual, attrs = _serialized_digests(stage / OWL_NAME)
        if actual != expected or attrs != original_attrs:
            raise ValueError("Serialized output does not exactly match the validated correction.")
        for path in inputs:
            if sha256(path) != hashes[str(path.resolve())]:
                raise ValueError(f"Input changed during processing: {path.name}.")
        root_attachments = sum(base + "MedicalHeading" in value for value in targets.values())
        report = {
            "mesh_release": str(mesh_release), "mesh_hierarchy_mode": "subject-categories",
            "class_interpretation": "subject_categories", "interpretation_note": SUBJECT_COMMENT,
            "input_sha256": hashes, "mesh_source": mesh["provenance"],
            "original_classes": len(by_iri), "total_output_classes": len(by_iri),
            "mesh_descriptors": len(descriptors),
            "mesh_broader_edges": sum(len(value) for value in parents.values()),
            "mesh_tree_positions": sum(len(d["tree_numbers"]) for d in descriptors.values()),
            "mesh_subclass_edges": sum(target.startswith(MESH) for value in targets.values() for target in value),
            "medical_heading_subclass_edges": root_attachments,
            "original_flat_edges_removed": flat_edges_removed,
            "original_medical_heading_subclass_edges": old_flat_edges,
            "original_mesh_hierarchy_edges": old_hierarchy_edges,
            "descriptors_without_tree_positions": sum(not d["tree_numbers"] for d in descriptors.values()),
            "descriptors_with_multiple_parents": sum(len(value) > 1 for value in parents.values()),
            "validation": {"xml_parses": True, "source_descriptor_ids_exact": True,
                           "source_tree_positions_exact": True, "source_broader_edges_exact": True,
                           "mesh_subclass_projection_exact": True, "original_non_hierarchy_content_preserved": True,
                           "serialized_content_exact": True, "input_hashes_unchanged": True,
                           "owl_reasoner_run": False, "disease_subsumption_asserted": False},
            "output_sha256": {OWL_NAME: sha256(stage / OWL_NAME)},
        }
        (stage / REPORT_NAME).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if output_dir.exists():
            raise ValueError("Output directory appeared during processing; refusing to replace it.")
        stage.rename(output_dir)
        published = True
        log(f"Finished: {report['mesh_subclass_edges']:,} MeSH parent edges and {root_attachments:,} MedicalHeading root attachments")
        return report
    finally:
        if not published:
            shutil.rmtree(stage, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owl", type=Path, required=True, help="Existing EMPO Step 3 OWL with native meshBroader annotations")
    parser.add_argument("--mesh-xml", type=Path, required=True, help="The original MeSH XML/ZIP/gzip recorded in the ontology")
    parser.add_argument("--output-dir", type=Path, required=True, help="Fresh output directory; must not exist")
    parser.add_argument("--mesh-release", default="2025")
    parser.add_argument("--mesh-member", help="Exact XML ZIP member if ambiguous")
    args = parser.parse_args()
    try:
        report = fix_hierarchy(args.owl, args.mesh_xml, args.output_dir,
                               mesh_release=args.mesh_release, member=args.mesh_member,
                               progress=lambda message: print(message, file=sys.stderr, flush=True))
    except (OSError, ValueError, ET.ParseError) as exc:
        parser.exit(2, f"MeSH hierarchy correction failed: {exc}\n")
    print(json.dumps({key: report[key] for key in ("mesh_descriptors", "mesh_subclass_edges", "medical_heading_subclass_edges")}, indent=2))
    print(f"Written: {args.output_dir / OWL_NAME}")


if __name__ == "__main__":
    main()
