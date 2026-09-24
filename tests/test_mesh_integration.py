"""MeSH integration checks with invented source descriptors and ICD classes."""

import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from integrate_mesh import integrate


BASE = "https://example.org/empo/"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"
MESH = "http://id.nlm.nih.gov/mesh/"
MESHV = "http://id.nlm.nih.gov/mesh/vocab#"
XML = "http://www.w3.org/XML/1998/namespace"


class MeSHIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.owl = self.folder / "input.owl"
        self.mesh = self.folder / "desc2025.xml.zip"
        self.mapping = self.folder / "icd2mesh.csv"
        self.write_owl()
        self.write_descriptors()
        self.rows = [
            {"ICD": "A001", "level": "level_1", "value": "A00", "mesh": "D000003", "mapping_type": "exact match"},
            {"ICD": "A002", "level": "level_1", "value": "a00", "mesh": "D000003", "mapping_type": "Sym"},
            {"ICD": "A001", "level": "level_2", "value": "A00.1", "mesh": "D000004", "mapping_type": "Equivalent"},
            {"ICD": "A001", "level": "level_2", "value": "A001", "mesh": "C000001", "mapping_type": "Loss of Information"},
            {"ICD": "B009", "level": "level_2", "value": "B009", "mesh": "D000003", "mapping_type": "Sym"},
            {"ICD": "S52501A", "level": "level_2", "value": "S52501A", "mesh": "D000003", "mapping_type": "Equivalent"},
        ]
        self.write_mapping()

    def write_owl(self):
        root = ET.Element(f"{{{RDF}}}RDF")
        ontology = ET.SubElement(root, f"{{{OWL}}}Ontology", {f"{{{RDF}}}about": BASE.rstrip("/")})
        ET.SubElement(ontology, f"{{{BASE}}}umlsAnnotationRelease").text = "2026AA"
        classes = [
            ("Observation", None, None, None),
            ("SpecialPopulation", None, None, None),
            ("Maternal", "SpecialPopulation", None, None),
            ("ICD_A00", "Observation", "code", "A00"),
            ("ICD_A001", "ICD_A00", "code", "A001"),
            ("ICD_A002", "ICD_A00", "code", "A002"),
            ("ICD_Group_S52501", "Observation", "analytical_group", "S52501"),
        ]
        for local, parent, kind, code in classes:
            node = ET.SubElement(root, f"{{{OWL}}}Class", {f"{{{RDF}}}about": BASE + local})
            ET.SubElement(node, f"{{{RDFS}}}label", {f"{{{XML}}}lang": "en"}).text = "Invented " + local
            if parent:
                ET.SubElement(node, f"{{{RDFS}}}subClassOf", {f"{{{RDF}}}resource": BASE + parent})
            if kind:
                ET.SubElement(node, f"{{{BASE}}}nodeKind").text = kind
                ET.SubElement(node, f"{{{BASE}}}ICD_Code").text = code
            if local == "ICD_A001":
                ET.SubElement(node, f"{{{BASE}}}observedInPopulation", {f"{{{RDF}}}resource": BASE + "Maternal"})
                ET.SubElement(node, f"{{{BASE}}}umlsCUI").text = "C1000001"
                ET.SubElement(node, f"{{{BASE}}}Synonyms").text = "Original UMLS synonym & <note>"
                ET.SubElement(node, f"{{{BASE}}}icdPreferredName").text = "Original ICD name"
            if local == "ICD_Group_S52501":
                ET.SubElement(node, f"{{{BASE}}}originalICDCode").text = "S52501A"
        ET.ElementTree(root).write(self.owl, encoding="utf-8", xml_declaration=True)

    def write_descriptors(self, mixed_root=False):
        root = ET.Element("DescriptorRecordSet", {"LanguageCode": "eng"})
        specs = [
            ("D000001", "Invented root one", ["C01"]),
            ("D000002", "Invented root two", ["C02"]),
            ("D000003", "Invented shared child & α", ["C01.100", "C02.200"] + (["C04"] if mixed_root else [])),
            ("D000004", "Invented grandchild", ["C01.100.100", "C02.200.100"]),
            ("D000005", "Unmapped independent descriptor", ["C03"]),
            ("D000006", "Descriptor without a tree", []),
        ]
        for ui, label, numbers in specs:
            rec = ET.SubElement(root, "DescriptorRecord", {"DescriptorClass": "1"})
            ET.SubElement(rec, "DescriptorUI").text = ui
            ET.SubElement(ET.SubElement(rec, "DescriptorName"), "String").text = label
            if numbers:
                trees = ET.SubElement(rec, "TreeNumberList")
                for number in numbers:
                    ET.SubElement(trees, "TreeNumber").text = number
            concepts = ET.SubElement(rec, "ConceptList")
            concept = ET.SubElement(concepts, "Concept", {"PreferredConceptYN": "Y"})
            ET.SubElement(concept, "ConceptUI").text = "M" + ui[1:]
            ET.SubElement(ET.SubElement(concept, "ConceptName"), "String").text = label
            ET.SubElement(concept, "ScopeNote").text = "Invented scope & definition for " + ui
            terms = ET.SubElement(concept, "TermList")
            term = ET.SubElement(terms, "Term", {"ConceptPreferredTermYN": "Y", "IsPermutedTermYN": "N", "LexicalTag": "NON", "PrintFlagYN": "Y", "RecordPreferredTermYN": "Y"})
            ET.SubElement(term, "TermUI").text = "T" + ui[1:]
            ET.SubElement(term, "String").text = label
        with zipfile.ZipFile(self.mesh, "w") as archive:
            header = b'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE DescriptorRecordSet SYSTEM "https://www.nlm.nih.gov/databases/dtd/nlmdescriptorrecordset_20250101.dtd">\n'
            archive.writestr("desc2025.xml", header + ET.tostring(root, encoding="utf-8"))
            archive.writestr("__MACOSX/._desc2025.xml", b"not a descriptor XML file")

    def write_mapping(self):
        with self.mapping.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, ["ICD", "level", "value", "mesh", "mapping_type"])
            writer.writeheader()
            writer.writerows(self.rows)

    @staticmethod
    def resources(node, namespace, local):
        return {child.get(f"{{{RDF}}}resource") for child in node.findall(f"{{{namespace}}}{local}")}

    @staticmethod
    def canonical(node):
        return (node.tag, tuple(sorted(node.attrib.items())), (node.text or "").strip(), tuple(MeSHIntegrationTests.canonical(child) for child in node))

    def run_integration(self, **kwargs):
        output = self.folder / ("output-" + str(len(list(self.folder.glob("output-*")))))
        report = integrate(self.owl, self.mesh, self.mapping, output, **kwargs)
        root = ET.parse(output / "empo_icd_umls_mesh.owl").getroot()
        classes = {c.get(f"{{{RDF}}}about"): c for c in root.findall(f"{{{OWL}}}Class")}
        return output, root, classes, report

    def review_file(self, rows):
        path = self.folder / "reviews.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, ["icd_code", "mesh_id", "decision", "reviewer", "evidence", "class_interpretation"])
            writer.writeheader()
            writer.writerows(rows)
        return path

    @staticmethod
    def read_csv(path):
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def test_original_icd_graph_populations_umls_and_input_bytes_preserved(self):
        before = {path: path.read_bytes() for path in (self.owl, self.mesh, self.mapping)}
        input_root = ET.fromstring(before[self.owl])
        _, _, classes, _ = self.run_integration()
        added = {f"{{{BASE}}}{name}" for name in ["candidateMeSH", "Has_MeSH"]}
        original_iris = {node.get(f"{{{RDF}}}about") for node in input_root.findall(f"{{{OWL}}}Class")}
        self.assertEqual(set(classes), original_iris | {BASE + "MedicalHeading"} | {MESH + f"D{number:06d}" for number in range(1, 7)})
        for original in input_root.findall(f"{{{OWL}}}Class"):
            iri = original.get(f"{{{RDF}}}about")
            current = classes[iri]
            self.assertEqual([self.canonical(c) for c in current if c.tag not in added], [self.canonical(c) for c in original])
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)

    def test_all_mesh_descriptors_and_native_multiple_parent_paths(self):
        output, root, classes, _ = self.run_integration()
        self.assertIn(BASE + "MedicalHeading", classes)
        for number in range(1, 7):
            iri = MESH + f"D{number:06d}"
            self.assertIn(iri, classes)
        for number in (1, 2, 5, 6):
            self.assertEqual(self.resources(classes[MESH + f"D{number:06d}"], RDFS, "subClassOf"), {BASE + "MedicalHeading"})
        child = classes[MESH + "D000003"]
        self.assertEqual(self.resources(child, RDFS, "subClassOf"), {MESH + "D000001", MESH + "D000002"})
        self.assertEqual(self.resources(child, BASE, "meshBroader"), {MESH + "D000001", MESH + "D000002"})
        grandchild = classes[MESH + "D000004"]
        self.assertEqual(self.resources(grandchild, BASE, "meshBroader"), {MESH + "D000003"})
        self.assertEqual(self.resources(grandchild, RDFS, "subClassOf"), {MESH + "D000003"})
        self.assertEqual({n.text for n in child.findall(f"{{{BASE}}}meshTreeNumber")}, {"C01.100", "C02.200"})
        tree_rows = self.read_csv(output / "mesh_hierarchy.csv")
        self.assertGreaterEqual(len(tree_rows), 4, "Distinct tree-position parent edges must survive descriptor-level parent deduplication")
        self.assertTrue(any("Invented shared child & α" == el.text for el in child.findall(f"{{{RDFS}}}label")))
        self.assertEqual(self.resources(classes[MESH + "D000006"], BASE, "meshBroader"), set())
        self.assertFalse(root.findall(f".//{{{OWL}}}equivalentClass"))

    def test_mixed_root_and_child_positions_keep_both_paths(self):
        self.write_descriptors(mixed_root=True)
        _, _, classes, _ = self.run_integration()
        child = classes[MESH + "D000003"]
        self.assertEqual(self.resources(child, RDFS, "subClassOf"), {BASE + "MedicalHeading", MESH + "D000001", MESH + "D000002"})
        self.assertEqual(self.resources(child, BASE, "meshBroader"), {MESH + "D000001", MESH + "D000002"})
        self.assertEqual({n.text for n in child.findall(f"{{{BASE}}}meshTreeNumber")}, {"C04", "C01.100", "C02.200"})

    def test_explicit_annotations_mode_preserves_legacy_flat_class_display(self):
        _, _, classes, _ = self.run_integration(mesh_hierarchy="annotations")
        for number in range(1, 7):
            self.assertEqual(self.resources(classes[MESH + f"D{number:06d}"], RDFS, "subClassOf"), {BASE + "MedicalHeading"})
        self.assertEqual(self.resources(classes[MESH + "D000003"], BASE, "meshBroader"), {MESH + "D000001", MESH + "D000002"})

    def test_maps_value_to_exact_existing_node_without_child_propagation(self):
        _, _, classes, _ = self.run_integration()
        self.assertEqual(self.resources(classes[BASE + "ICD_A00"], BASE, "candidateMeSH"), {MESH + "D000003"})
        self.assertEqual(self.resources(classes[BASE + "ICD_A001"], BASE, "candidateMeSH"), {MESH + "D000004"})
        self.assertEqual(self.resources(classes[BASE + "ICD_A002"], BASE, "candidateMeSH"), set())
        self.assertEqual(self.resources(classes[BASE + "ICD_Group_S52501"], BASE, "candidateMeSH"), set())
        self.assertNotIn(BASE + "ICD_S52501A", classes)

    def test_mapping_labels_never_create_accepted_or_subclass_links_by_default(self):
        _, _, classes, _ = self.run_integration()
        for iri, node in classes.items():
            if iri.startswith(BASE + "ICD_"):
                self.assertEqual(self.resources(node, BASE, "Has_MeSH"), set())
                self.assertFalse(any(parent.startswith(MESH) for parent in self.resources(node, RDFS, "subClassOf")))

    def test_missing_supplementary_and_unknown_codes_remain_audited(self):
        output, _, classes, _ = self.run_integration()
        mappings = self.read_csv(output / "icd_mesh_mappings.csv")
        statuses = {row["status"] for row in mappings}
        self.assertIn("missing_supplementary_record", statuses)
        self.assertIn("missing_icd_code", statuses)
        self.assertNotIn(MESH + "C000001", classes)
        self.assertFalse(any("B009" in iri for iri in classes))
        audit = self.read_csv(output / "icd_mesh_row_audit.csv")
        self.assertEqual(len(audit), len(self.rows))
        self.assertEqual(len(mappings), 5, "Two original codes mapping through one normalized value form one pair")
        self.assertIn("Sym", (output / "icd_mesh_row_audit.csv").read_text())

    def test_missing_descriptor_and_unknown_mapping_label_are_reported(self):
        self.rows.append({"ICD": "A001", "level": "level_2", "value": "A001", "mesh": "D999999", "mapping_type": "Undocumented label"})
        self.write_mapping()
        output, _, classes, _ = self.run_integration()
        mappings = self.read_csv(output / "icd_mesh_mappings.csv")
        self.assertIn("missing_mesh_descriptor", {row["status"] for row in mappings})
        self.assertIn("Undocumented label", (output / "icd_mesh_row_audit.csv").read_text())
        self.assertNotIn(MESH + "D999999", classes)

    def test_explicit_review_accepts_link_or_rejects_candidate(self):
        reviews = self.review_file([
            {"icd_code": "A00", "mesh_id": "D000003", "decision": "accept_link", "reviewer": "Test reviewer", "evidence": "Verified source match in invented fixture"},
            {"icd_code": "A001", "mesh_id": "D000004", "decision": "reject", "reviewer": "Test reviewer", "evidence": "Scope mismatch in invented fixture"},
        ])
        _, _, classes, _ = self.run_integration(review_csv=reviews)
        self.assertEqual(self.resources(classes[BASE + "ICD_A00"], BASE, "Has_MeSH"), {MESH + "D000003"})
        self.assertEqual(self.resources(classes[BASE + "ICD_A00"], BASE, "candidateMeSH"), set())
        self.assertEqual(self.resources(classes[BASE + "ICD_A001"], BASE, "Has_MeSH"), set())
        self.assertEqual(self.resources(classes[BASE + "ICD_A001"], BASE, "candidateMeSH"), set())
        self.assertEqual(self.resources(classes[BASE + "ICD_A00"], RDFS, "subClassOf"), {BASE + "Observation"})

    def test_subclass_requires_review_and_explicit_disease_interpretation(self):
        row = {"icd_code": "A001", "mesh_id": "D000004", "decision": "subclass", "reviewer": "Test reviewer", "evidence": "Every source disease is an instance of the target disease"}
        reviews = self.review_file([row])
        with self.assertRaises(ValueError):
            self.run_integration(review_csv=reviews, mesh_hierarchy="annotations")
        row["class_interpretation"] = "disease_concepts"
        reviews = self.review_file([row])
        with self.assertRaises(ValueError):
            self.run_integration(review_csv=reviews)
        _, _, classes, _ = self.run_integration(review_csv=reviews, mesh_hierarchy="annotations")
        self.assertEqual(self.resources(classes[BASE + "ICD_A001"], RDFS, "subClassOf"), {BASE + "ICD_A00", MESH + "D000004"})
        self.assertIn(MESH + "D000004", self.resources(classes[BASE + "ICD_A001"], BASE, "Has_MeSH"))

    def test_review_missing_evidence_unknown_pair_or_conflicting_decisions_fail(self):
        valid = {"icd_code": "A00", "mesh_id": "D000003", "decision": "accept_link", "reviewer": "Test reviewer", "evidence": "Fixture evidence"}
        for changed in [{"evidence": ""}, {"reviewer": ""}, {"icd_code": "A002"}, {"decision": "Equivalent"}]:
            with self.subTest(changed=changed):
                reviews = self.review_file([dict(valid, **changed)])
                with self.assertRaises(ValueError):
                    self.run_integration(review_csv=reviews)
        reviews = self.review_file([valid, dict(valid, decision="reject")])
        with self.assertRaises(ValueError):
            self.run_integration(review_csv=reviews)

    def test_blank_review_template_is_no_op(self):
        review = self.review_file([{"icd_code": "A00", "mesh_id": "D000003", "decision": "", "reviewer": "", "evidence": ""}])
        _, _, classes, _ = self.run_integration(review_csv=review)
        self.assertEqual(self.resources(classes[BASE + "ICD_A00"], BASE, "candidateMeSH"), {MESH + "D000003"})
        self.assertEqual(self.resources(classes[BASE + "ICD_A00"], BASE, "Has_MeSH"), set())

    def test_duplicate_existing_official_code_is_not_arbitrarily_selected(self):
        tree = ET.parse(self.owl)
        node = ET.SubElement(tree.getroot(), f"{{{OWL}}}Class", {f"{{{RDF}}}about": BASE + "Second_A00"})
        ET.SubElement(node, f"{{{RDFS}}}label").text = "Conflicting official code node"
        ET.SubElement(node, f"{{{BASE}}}nodeKind").text = "code"
        ET.SubElement(node, f"{{{BASE}}}ICD_Code").text = "A00"
        tree.write(self.owl, encoding="utf-8", xml_declaration=True)
        with self.assertRaises(ValueError):
            self.run_integration()

    def test_unavailable_endpoints_cannot_be_approved_but_can_be_rejected(self):
        pairs = [("A001", "C000001"), ("B009", "D000003")]
        for code, mesh_id in pairs:
            for decision in ["accept_link", "subclass"]:
                with self.subTest(code=code, mesh_id=mesh_id, decision=decision):
                    reviews = self.review_file([{"icd_code": code, "mesh_id": mesh_id, "decision": decision, "reviewer": "Test reviewer", "evidence": "Review cannot materialize absent endpoint", "class_interpretation": "disease_concepts"}])
                    with self.assertRaises(ValueError):
                        self.run_integration(review_csv=reviews, mesh_hierarchy="annotations")
        reviews = self.review_file([{"icd_code": code, "mesh_id": mesh_id, "decision": "reject", "reviewer": "Test reviewer", "evidence": "Unsupported endpoint"} for code, mesh_id in pairs])
        output, _, classes, _ = self.run_integration(review_csv=reviews)
        statuses = [row["status"] for row in self.read_csv(output / "icd_mesh_mappings.csv")]
        self.assertEqual(statuses.count("rejected"), 2)
        self.assertNotIn(MESH + "C000001", classes)

    def test_refuses_existing_output_directory_and_annotated_input(self):
        output = self.folder / "existing"
        output.mkdir()
        with self.assertRaises((ValueError, FileExistsError)):
            integrate(self.owl, self.mesh, self.mapping, output)
        self.assertEqual(list(output.iterdir()), [])
        output, _, _, _ = self.run_integration()
        with self.assertRaises(ValueError):
            integrate(output / "empo_icd_umls_mesh.owl", self.mesh, self.mapping, self.folder / "rerun")
        self.assertFalse((self.folder / "rerun").exists())

    def test_failure_during_serialization_leaves_no_partial_output(self):
        output = self.folder / "interrupted"
        original_write = ET.ElementTree.write

        def broken_write(tree, destination, *args, **kwargs):
            if str(destination).endswith(".owl"):
                Path(destination).write_text("<rdf:RDF>partial")
                raise OSError("simulated interrupted output")
            return original_write(tree, destination, *args, **kwargs)

        before = set(self.folder.iterdir())
        with patch("integrate_mesh.ET.ElementTree.write", broken_write):
            with self.assertRaises(OSError):
                integrate(self.owl, self.mesh, self.mapping, output)
        self.assertFalse(output.exists())
        self.assertEqual(set(self.folder.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
