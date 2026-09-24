"""Existing MeSH output can acquire a visible tree without changing mappings."""

from collections import Counter
from pathlib import Path
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

import test_mesh_integration as fixtures
from fix_mesh_hierarchy import fix_hierarchy

BASE, MESH = fixtures.BASE, fixtures.MESH
RDF, RDFS, OWL = fixtures.RDF, fixtures.RDFS, fixtures.OWL
ABOUT, RESOURCE = f"{{{RDF}}}about", f"{{{RDF}}}resource"
CLASS, SUBCLASS = f"{{{OWL}}}Class", f"{{{RDFS}}}subClassOf"


class FixMeSHHierarchyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MeSHIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.folder = self.fixture.folder
        self.mesh = self.fixture.mesh

    def legacy_output(self, **kwargs):
        folder, root, classes, report = self.fixture.run_integration(mesh_hierarchy="annotations", **kwargs)
        return folder / "empo_icd_umls_mesh.owl"

    def fixed_output(self, source):
        folder = self.folder / "fixed"
        report = fix_hierarchy(source, self.mesh, folder)
        path = folder / "empo_icd_umls_mesh.owl"
        root = ET.parse(path).getroot()
        classes = {node.get(ABOUT): node for node in root.findall(CLASS)}
        return path, root, classes, report

    @staticmethod
    def resources(node, namespace, local):
        return fixtures.MeSHIntegrationTests.resources(node, namespace, local)

    @staticmethod
    def canonical(node):
        return fixtures.MeSHIntegrationTests.canonical(node)

    def test_preserves_icd_umls_population_mapping_axioms_and_source_bytes(self):
        review = self.fixture.review_file([
            {"icd_code": "A00", "mesh_id": "D000003", "decision": "accept_link", "reviewer": "Fixture reviewer", "evidence": "Accepted test mapping"}
        ])
        source = self.legacy_output(review_csv=review)
        source_bytes, mesh_bytes = source.read_bytes(), self.mesh.read_bytes()
        original = ET.fromstring(source_bytes)
        _, root, classes, report = self.fixed_output(source)
        originals = {node.get(ABOUT): node for node in original.findall(CLASS)}
        self.assertEqual(set(classes), set(originals))
        for iri, node in originals.items():
            old_children = list(node)
            new_children = list(classes[iri])
            if iri.startswith(MESH):
                old_children = [c for c in old_children if c.tag != SUBCLASS]
                new_children = [c for c in new_children if c.tag not in {SUBCLASS, f"{{{BASE}}}meshClassInterpretation"}]
                self.assertEqual([c.text for c in classes[iri].findall(f"{{{BASE}}}meshClassInterpretation")], ["subject_categories"])
            if iri == BASE + "MedicalHeading":
                self.assertFalse(Counter(map(self.canonical, old_children)) - Counter(map(self.canonical, new_children)))
            else:
                self.assertEqual(list(map(self.canonical, old_children)), list(map(self.canonical, new_children)), iri)
        old_elements = [self.canonical(n) for n in original if n.tag not in {CLASS, f"{{{OWL}}}Ontology"}]
        new_elements = [self.canonical(n) for n in root if n.tag not in {CLASS, f"{{{OWL}}}Ontology"}]
        self.assertFalse(Counter(old_elements) - Counter(new_elements), "Existing property declarations and mapping evidence must survive")
        old_ontology = original.find(f"{{{OWL}}}Ontology")
        new_ontology = root.find(f"{{{OWL}}}Ontology")
        old_ontology_content = [n for n in old_ontology if n.tag != f"{{{BASE}}}meshHierarchyMode"]
        self.assertFalse(Counter(map(self.canonical, old_ontology_content)) - Counter(map(self.canonical, new_ontology)))
        self.assertEqual([n.text for n in new_ontology.findall(f"{{{BASE}}}meshHierarchyMode")], ["subject-categories"])
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual(self.mesh.read_bytes(), mesh_bytes)
        self.assertEqual(self.resources(classes[BASE + "ICD_A00"], BASE, "Has_MeSH"), {MESH + "D000003"})
        self.assertEqual(self.resources(classes[BASE + "ICD_A001"], BASE, "candidateMeSH"), {MESH + "D000004"})
        self.assertEqual(self.resources(classes[BASE + "ICD_A001"], BASE, "observedInPopulation"), {BASE + "Maternal"})
        self.assertEqual(self.resources(classes[MESH + "D000003"], RDFS, "subClassOf"), {MESH + "D000001", MESH + "D000002"})
        self.assertEqual(self.resources(classes[MESH + "D000004"], RDFS, "subClassOf"), {MESH + "D000003"})
        for number in (1, 2, 5, 6):
            self.assertEqual(self.resources(classes[MESH + f"D{number:06d}"], RDFS, "subClassOf"), {BASE + "MedicalHeading"})
        self.assertEqual(report["mesh_descriptors"], 6)
        self.assertEqual(report["mesh_broader_edges"], 3)
        self.assertEqual(report["mesh_subclass_edges"], 3)
        self.assertEqual(report["medical_heading_subclass_edges"], 4)
        self.assertEqual(report["original_flat_edges_removed"], 2)
        self.assertTrue((self.folder / "fixed" / "mesh_hierarchy_fix_report.json").is_file())

    def test_mixed_root_and_child_positions_remain_visible_in_both_places(self):
        self.fixture.write_descriptors(mixed_root=True)
        source = self.legacy_output()
        _, _, classes, _ = self.fixed_output(source)
        child = classes[MESH + "D000003"]
        self.assertEqual(self.resources(child, RDFS, "subClassOf"), {BASE + "MedicalHeading", MESH + "D000001", MESH + "D000002"})
        self.assertEqual(self.resources(child, BASE, "meshBroader"), {MESH + "D000001", MESH + "D000002"})
        self.assertEqual({c.text for c in child.findall(f"{{{BASE}}}meshTreeNumber")}, {"C04", "C01.100", "C02.200"})

    def test_different_source_archive_is_rejected_even_if_descriptors_match(self):
        source = self.legacy_output()
        with zipfile.ZipFile(self.mesh, "a") as archive:
            archive.comment = b"Different source archive with identical descriptor member"
        with self.assertRaises(ValueError):
            self.fixed_output(source)
        self.assertFalse((self.folder / "fixed").exists())

    def test_missing_descriptor_or_changed_tree_or_broader_links_are_rejected(self):
        source = self.legacy_output()
        original_bytes = source.read_bytes()
        for change in ("descriptor", "tree", "broader"):
            with self.subTest(change=change):
                root = ET.fromstring(original_bytes)
                child = next(n for n in root.findall(CLASS) if n.get(ABOUT) == MESH + "D000003")
                if change == "descriptor":
                    root.remove(child)
                elif change == "tree":
                    child.find(f"{{{BASE}}}meshTreeNumber").text = "C01.999"
                else:
                    child.remove(child.find(f"{{{BASE}}}meshBroader"))
                ET.ElementTree(root).write(source, encoding="utf-8", xml_declaration=True)
                with self.assertRaises(ValueError):
                    self.fixed_output(source)
                self.assertFalse((self.folder / "fixed").exists())

    def test_refuses_existing_disease_subclass_mapping(self):
        review = self.fixture.review_file([
            {"icd_code": "A00", "mesh_id": "D000003", "decision": "subclass", "reviewer": "Fixture reviewer", "evidence": "Reviewed as disease class", "class_interpretation": "disease_concepts"}
        ])
        source = self.legacy_output(review_csv=review)
        with self.assertRaises(ValueError):
            self.fixed_output(source)
        self.assertFalse((self.folder / "fixed").exists())

    def test_refuses_repeated_projection_and_existing_destination(self):
        source = self.legacy_output()
        fixed_path, _, _, _ = self.fixed_output(source)
        with self.assertRaises(ValueError):
            fix_hierarchy(fixed_path, self.mesh, self.folder / "fixed-again")
        self.assertFalse((self.folder / "fixed-again").exists())
        empty = self.folder / "existing"
        empty.mkdir()
        with self.assertRaises((ValueError, FileExistsError)):
            fix_hierarchy(source, self.mesh, empty)
        self.assertEqual(list(empty.iterdir()), [])

    def test_interrupted_serialization_leaves_no_partial_output(self):
        source = self.legacy_output()
        before = set(self.folder.iterdir())
        original_write = ET.ElementTree.write

        def broken_write(tree, destination, *args, **kwargs):
            if str(destination).endswith(".owl"):
                Path(destination).write_text("<rdf:RDF>partial")
                raise OSError("simulated interrupted output")
            return original_write(tree, destination, *args, **kwargs)

        with patch("fix_mesh_hierarchy.ET.ElementTree.write", broken_write):
            with self.assertRaises(OSError):
                self.fixed_output(source)
        self.assertFalse((self.folder / "fixed").exists())
        self.assertEqual(set(self.folder.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
