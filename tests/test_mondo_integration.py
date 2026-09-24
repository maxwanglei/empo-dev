"""End-to-end invariants for the MONDO human disease / ICD integration."""

import csv
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

from integrate_mondo import integrate

try:  # Support unittest discovery and direct test-module execution.
    from test_mondo_reader import HUMAN, MONDO, OBO, OIO, OWL, RDF, RDFS, XML, disease, sample_document
except ModuleNotFoundError:
    from tests.test_mondo_reader import HUMAN, MONDO, OBO, OIO, OWL, RDF, RDFS, XML, disease, sample_document


BASE = "https://example.org/empo/"
MESH = "http://id.nlm.nih.gov/mesh/"
ABOUT = f"{{{RDF}}}about"
RESOURCE = f"{{{RDF}}}resource"


class MondoIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.owl = self.folder / "empo_icd_umls_mesh.owl"
        self.mondo = self.folder / "mondo.owl.zip"
        self.write_input()
        self.write_mondo()

    def write_input(self):
        root = ET.Element(f"{{{RDF}}}RDF")
        ontology = ET.SubElement(root, f"{{{OWL}}}Ontology", {ABOUT: BASE.rstrip("/")})
        ET.SubElement(ontology, f"{{{BASE}}}umlsAnnotationRelease").text = "2026AA"
        ET.SubElement(ontology, f"{{{BASE}}}meshAnnotationRelease").text = "2025"
        ET.SubElement(ontology, f"{{{BASE}}}meshHierarchyMode").text = "subject-categories"
        specs = [
            ("Observation", None, None, None),
            ("SpecialPopulation", None, None, None),
            ("Maternal", "SpecialPopulation", None, None),
            ("MedicalHeading", None, None, None),
            ("ICD_Chapter_A00B99", "Observation", "chapter", "A00-B99"),
            ("ICD_Block_A00A09", "ICD_Chapter_A00B99", "block", "A00-A09"),
            ("ICD_Block_A00", "ICD_Block_A00A09", "block", "A00"),
            ("ICD_A00", "ICD_Block_A00", "code", "A00"),
            ("ICD_A001", "ICD_A00", "code", "A001"),
            ("ICD_A002", "ICD_A00", "code", "A002"),
            ("ICD_A003", "ICD_A00", "code", "A003"),
            ("ICD_Group_S52501", "Observation", "analytical_group", "S52501"),
            ("ICD_Unresolved_QZZ", "Observation", "unresolved_group", "QZZ"),
        ]
        for local, parent, kind, code in specs:
            node = ET.SubElement(root, f"{{{OWL}}}Class", {ABOUT: BASE + local})
            ET.SubElement(node, f"{{{RDFS}}}label", {f"{{{XML}}}lang": "en"}).text = "Invented " + local
            if parent:
                ET.SubElement(node, f"{{{RDFS}}}subClassOf", {RESOURCE: BASE + parent})
            if kind:
                ET.SubElement(node, f"{{{BASE}}}nodeKind").text = kind
                if local == "ICD_Chapter_A00B99":
                    ET.SubElement(node, f"{{{BASE}}}icdPreferredName").text = "Invented original chapter (A00-B99)"
                elif local != "ICD_Block_A00":
                    ET.SubElement(node, f"{{{BASE}}}ICD_Code").text = code
            if local == "ICD_A001":
                ET.SubElement(node, f"{{{BASE}}}observedInPopulation", {RESOURCE: BASE + "Maternal"})
                ET.SubElement(node, f"{{{BASE}}}umlsCUI").text = "C1000001"
                ET.SubElement(node, f"{{{BASE}}}Synonyms").text = "Original UMLS & synonym"
                ET.SubElement(node, f"{{{BASE}}}candidateMeSH", {RESOURCE: MESH + "D000002"})
            if local == "ICD_Group_S52501":
                ET.SubElement(node, f"{{{BASE}}}originalICDCode").text = "S52501A"
        for identifier, parent in [("D000001", BASE + "MedicalHeading"), ("D000002", MESH + "D000001")]:
            node = ET.SubElement(root, f"{{{OWL}}}Class", {ABOUT: MESH + identifier})
            ET.SubElement(node, f"{{{RDFS}}}label").text = "Invented heading " + identifier
            ET.SubElement(node, f"{{{RDFS}}}subClassOf", {RESOURCE: parent})
            ET.SubElement(node, f"{{{BASE}}}nodeKind").text = "mesh_descriptor"
            if identifier == "D000002":
                ET.SubElement(node, f"{{{BASE}}}meshBroader", {RESOURCE: parent})
                ET.SubElement(node, f"{{{BASE}}}meshTreeNumber").text = "C01.100"
        ET.ElementTree(root).write(self.owl, encoding="utf-8", xml_declaration=True)

    def write_mondo(self):
        root = ET.fromstring(sample_document())
        node = ET.SubElement(root, f"{{{OWL}}}Class", {ABOUT: MONDO + "9000005"})
        ET.SubElement(node, f"{{{RDFS}}}label").text = "Invented mapped disease"
        ET.SubElement(node, f"{{{RDFS}}}subClassOf", {RESOURCE: HUMAN})
        for target in ("ICD10CM:A00", "ICD10CM:A00-A00", "ICD10CM:A00-A09", "ICD10CM:A00-B99", "ICD10CM:A00.9",
                       "ICD10CM:S52.501A", "ICD10CM:S52.501", "ICD10CM:QZZ"):
            ET.SubElement(node, f"{{{OIO}}}hasDbXref").text = target
        with zipfile.ZipFile(self.mondo, "w") as archive:
            archive.writestr("mondo.owl", ET.tostring(root, encoding="utf-8", xml_declaration=True))
            archive.writestr("__MACOSX/._mondo.owl", b"not XML")

    @staticmethod
    def canonical(node):
        return (node.tag, tuple(sorted(node.attrib.items())), (node.text or "").strip(),
                tuple(MondoIntegrationTests.canonical(child) for child in node))

    @staticmethod
    def resources(node, namespace, local):
        return {child.get(RESOURCE) for child in node.findall(f"{{{namespace}}}{local}")}

    @staticmethod
    def csv(path):
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def run_integration(self):
        output = self.folder / ("output-" + str(len(list(self.folder.glob("output-*")))))
        report = integrate(self.owl, self.mondo, output)
        root = ET.parse(output / "empo_icd_umls_mesh_mondo.owl").getroot()
        classes = {node.get(ABOUT): node for node in root.findall(f"{{{OWL}}}Class")}
        return output, root, classes, report

    def test_input_bytes_and_all_existing_class_content_preserved(self):
        before = {path: path.read_bytes() for path in (self.owl, self.mondo)}
        source = ET.fromstring(before[self.owl])
        _, _, classes, _ = self.run_integration()
        for original in source.findall(f"{{{OWL}}}Class"):
            self.assertEqual(self.canonical(original), self.canonical(classes[original.get(ABOUT)]))
        for path, content in before.items():
            self.assertEqual(content, path.read_bytes())

    def test_human_root_and_native_multiple_parents_are_not_flattened(self):
        output, root, classes, _ = self.run_integration()
        selected = {HUMAN, *(MONDO + n for n in ("9000001", "9000002", "9000003", "9000005"))}
        self.assertEqual({iri for iri in classes if iri.startswith(MONDO)}, selected)
        self.assertEqual(self.resources(classes[HUMAN], RDFS, "subClassOf"), set())
        self.assertEqual(self.resources(classes[MONDO + "9000003"], RDFS, "subClassOf"), {MONDO + "9000001", MONDO + "9000002"})
        hierarchy = self.csv(output / "mondo_hierarchy.csv")
        self.assertEqual({(row["child_iri"], row["parent_iri"]) for row in hierarchy},
                         {(MONDO + "9000001", HUMAN), (MONDO + "9000002", HUMAN),
                          (MONDO + "9000003", MONDO + "9000001"),
                          (MONDO + "9000003", MONDO + "9000002"), (MONDO + "9000005", HUMAN)})
        self.assertEqual(len(self.csv(output / "mondo_human_terms.csv")), 5)
        self.assertFalse(root.findall(f".//{{{OWL}}}imports"))
        self.assertNotIn(MONDO + "9000004", classes)
        self.assertNotIn(MONDO + "9000099", classes)

    def test_direct_mondo_mappings_use_exact_icd10cm_identity_at_all_levels(self):
        output, _, classes, _ = self.run_integration()
        self.assertEqual(self.resources(classes[MONDO + "9000001"], BASE, "hasICDMapping"), {BASE + "ICD_A001"})
        self.assertEqual(self.resources(classes[MONDO + "9000003"], BASE, "hasICDMapping"), {BASE + "ICD_A002"})
        self.assertEqual(self.resources(classes[MONDO + "9000005"], BASE, "hasICDMapping"),
                         {BASE + "ICD_A00", BASE + "ICD_Block_A00", BASE + "ICD_Block_A00A09", BASE + "ICD_Chapter_A00B99"})
        rows = self.csv(output / "mondo_icd_mappings.csv")
        singleton = next(row for row in rows if row["source_target"] == "ICD10CM:A00")
        self.assertEqual(set(json.loads(singleton["matched_node_kinds"])), {"code"})
        singleton_range = next(row for row in rows if row["source_target"] == "ICD10CM:A00-A00")
        self.assertEqual(set(json.loads(singleton_range["matched_node_kinds"])), {"block"})
        self.assertEqual(singleton["status"], "linked")
        self.assertIn("unsupported_icd_namespace", {row["status"] for row in rows})
        self.assertIn("absent_icd_endpoint", {row["status"] for row in rows})
        self.assertIn("invalid_icd_identifier", {row["status"] for row in rows})

    def test_no_conflation_truncation_descendant_propagation_or_synthetic_matches(self):
        output, _, classes, _ = self.run_integration()
        links = {(iri, target) for iri, node in classes.items() for target in self.resources(node, BASE, "hasICDMapping")}
        self.assertNotIn((MONDO + "9000001", BASE + "ICD_A002"), links, "ICD10 is not ICD10CM")
        self.assertFalse(any(target == BASE + "ICD_A003" for _, target in links), "An ancestor mapping must not propagate to children")
        self.assertFalse(any("ICD_Group_" in target or "ICD_Unresolved_" in target for _, target in links))
        self.assertFalse(any("S52501A" in iri for iri in classes), "Missing seven-character endpoints must not create code nodes")
        rows = self.csv(output / "mondo_icd_mappings.csv")
        for code in ("ICD10CM:S52.501A", "ICD10CM:S52.501"):
            self.assertEqual(next(row["status"] for row in rows if row["source_target"] == code), "absent_icd_endpoint")

    def test_all_icd_node_kinds_have_coverage_and_unmapped_nodes_remain(self):
        output, _, _, _ = self.run_integration()
        rows = self.csv(output / "icd_mondo_coverage.csv")
        self.assertEqual(len(rows), 9)
        self.assertEqual({row["node_kind"] for row in rows}, {"code", "chapter", "block", "analytical_group", "unresolved_group"})
        counts = {row["class_iri"]: int(row["mondo_mapping_count"]) for row in rows}
        self.assertEqual(counts[BASE + "ICD_A001"], 1)
        self.assertEqual(counts[BASE + "ICD_A003"], 0)
        self.assertEqual(counts[BASE + "ICD_Group_S52501"], 0)
        self.assertEqual(counts[BASE + "ICD_Unresolved_QZZ"], 0)

    def test_ambiguous_range_is_reported_without_picking_one_endpoint(self):
        tree = ET.parse(self.owl)
        node = ET.SubElement(tree.getroot(), f"{{{OWL}}}Class", {ABOUT: BASE + "ICD_Block_DuplicateRange"})
        ET.SubElement(node, f"{{{RDFS}}}label").text = "Invented duplicate range"
        ET.SubElement(node, f"{{{BASE}}}nodeKind").text = "block"
        ET.SubElement(node, f"{{{BASE}}}ICD_Code").text = "A00-A09"
        ET.SubElement(node, f"{{{RDFS}}}subClassOf", {RESOURCE: BASE + "Observation"})
        tree.write(self.owl, encoding="utf-8", xml_declaration=True)
        output, _, classes, _ = self.run_integration()
        row = next(row for row in self.csv(output / "mondo_icd_mappings.csv") if row["source_target"] == "ICD10CM:A00-A09")
        self.assertEqual(row["status"], "ambiguous_icd_endpoint")
        links = self.resources(classes[MONDO + "9000005"], BASE, "hasICDMapping")
        self.assertNotIn(BASE + "ICD_Block_A00A09", links)
        self.assertNotIn(BASE + "ICD_Block_DuplicateRange", links)

    def test_synonym_types_definition_and_mapping_evidence_survive(self):
        output, root, classes, _ = self.run_integration()
        node = classes[MONDO + "9000003"]
        for kind in ("Exact", "Broad", "Narrow", "Related"):
            self.assertEqual(node.findtext(f"{{{OIO}}}has{kind}Synonym"), kind + " invented name")
        self.assertEqual(node.findtext(f"{{{OBO}}}IAO_0000115"), "Invented definition & provenance.")
        self.assertEqual(node.find(f"{{{OIO}}}hasExactSynonym").get(f"{{{XML}}}lang"), "en")
        serialized = ET.tostring(root, encoding="unicode")
        self.assertIn("Invented curator evidence", serialized)
        self.assertIn("Invented synonym citation", serialized)
        self.assertIn("equivalentTo", serialized)
        rows = self.csv(output / "mondo_icd_mappings.csv")
        mapped = next(row for row in rows if row["source_target"] == "ICD10CM:A00.1")
        self.assertEqual(mapped["source_predicate"], OIO + "hasDbXref")
        self.assertIn("Invented curator evidence", mapped["source_qualifiers"])

    def test_cross_vocabulary_links_are_annotations_without_extra_mesh_or_umls_maps(self):
        _, root, classes, _ = self.run_integration()
        property_node = next(n for n in root.findall(f"{{{OWL}}}AnnotationProperty") if n.get(ABOUT) == BASE + "hasICDMapping")
        self.assertIsNotNone(property_node)
        self.assertFalse(root.findall(f".//{{{OWL}}}equivalentClass"))
        for iri, node in classes.items():
            if iri.startswith(MONDO):
                self.assertTrue(all(parent.startswith(MONDO) for parent in self.resources(node, RDFS, "subClassOf")))
                self.assertFalse(node.findall(f"{{{BASE}}}observedInPopulation"))
                self.assertFalse(node.findall(f"{{{BASE}}}candidateMeSH"))
                self.assertFalse(node.findall(f"{{{BASE}}}Has_MeSH"))
            else:
                self.assertFalse(any(parent.startswith(MONDO) for parent in self.resources(node, RDFS, "subClassOf")))
        self.assertEqual(classes[MONDO + "9000001"].findtext(f"{{{OIO}}}hasDbXref"), "ICD10CM:A00.1")

    def test_existing_output_is_not_overwritten(self):
        output = self.folder / "existing"
        output.mkdir()
        marker = output / "marker.txt"
        marker.write_text("keep")
        with self.assertRaises((ValueError, FileExistsError)):
            integrate(self.owl, self.mondo, output)
        self.assertEqual(marker.read_text(), "keep")
        self.assertEqual(list(output.iterdir()), [marker])

    def test_already_integrated_input_is_rejected(self):
        output, _, _, _ = self.run_integration()
        rerun = self.folder / "rerun"
        with self.assertRaises(ValueError):
            integrate(output / "empo_icd_umls_mesh_mondo.owl", self.mondo, rerun)
        self.assertFalse(rerun.exists())

    def test_invalid_source_leaves_no_partial_output(self):
        self.mondo.write_bytes(b"not a ZIP archive or RDF/XML")
        output = self.folder / "bad-source"
        with self.assertRaises((ValueError, zipfile.BadZipFile)):
            integrate(self.owl, self.mondo, output)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
