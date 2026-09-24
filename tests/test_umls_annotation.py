"""Local UMLS annotation checks with invented OWL classes and MRCONSO atoms."""

import csv
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from annotate_umls import annotate


BASE = "https://example.org/empo/"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"
XML = "http://www.w3.org/XML/1998/namespace"
FIELDS = "CUI LAT TS LUI STT SUI ISPREF AUI SAUI SCUI SDUI SAB TTY CODE STR SRL SUPPRESS CVF".split()
PREFERRED = "Invented preferred & <α> condition"
SYNONYM = 'Invented alternate "name" & observation'


def atom(cui, code, text, aui, **changes):
    row = dict(zip(FIELDS, [cui, "ENG", "S", "L0000001", "VC", "S0000001", "N", aui, "", "", "", "ICD10CM", "PT", code, text, "0", "N", ""]))
    row.update(changes)
    return row


class UMLSAnnotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.input = self.folder / "synthetic_backbone.owl"
        self.archive = self.folder / "synthetic_mrconso.zip"
        self.atoms = [
            atom("C0000001", "A00.1", "Invented ICD source atom", "A0000001"),
            atom("C0000001", "101", PREFERRED, "A0000002", SAB="SNOMEDCT_US", TS="P", STT="PF", ISPREF="Y"),
            atom("C0000001", "D001", SYNONYM, "A0000003", SAB="MSH"),
            atom("C0000001", "102", "ISPREF alone is insufficient", "A0000004", SAB="SNOMEDCT_US", STT="PF", ISPREF="Y"),
            atom("C0000001", "103", "Excluded non-English atom", "A0000005", SAB="SNOMEDCT_US", LAT="SPA"),
            atom("C0000001", "104", "Excluded suppressed atom", "A0000006", SAB="SNOMEDCT_US", SUPPRESS="Y"),
            atom("C0000002", "A00.2", "Invented second code", "A0000007"),
            atom("C0000002", "B00.1", "First ambiguous anchor", "A0000008"),
            atom("C0000003", "B00.1", "Second ambiguous anchor", "A0000009"),
            atom("C0000002", "201", "CUI two preferred name", "A0000010", SAB="SNOMEDCT_US", TS="P", STT="PF", ISPREF="Y"),
            atom("C0000004", "S52.501", "Never attach to synthetic group", "A0000011"),
            atom("C0000005", "S52.501A", "Never pool constituent names", "A0000012"),
            atom("C0000006", "T00.000", "Never attach to unresolved group", "A0000013"),
            atom("C0000010", "A00", "Ancestor source atom", "A0000014"),
            atom("C0000011", "A00-B99", "Chapter range source atom", "A0000015"),
            atom("C0000012", "A00-A09", "Block range source atom", "A0000016"),
        ]
        self.write_owl()
        self.write_zip()

    def write_zip(self, extra=""):
        text = "".join("|".join(a[k] for k in FIELDS) + "|\n" for a in self.atoms) + extra
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr("synthetic/META/MRCONSO.RRF", text)

    def write_owl(self):
        root = ET.Element(f"{{{RDF}}}RDF")
        ET.SubElement(root, f"{{{OWL}}}Ontology", {f"{{{RDF}}}about": BASE.rstrip("/")})
        classes = [
            ("Observation", "Observation", None, None, None),
            ("SpecialPopulation", "Population", None, None, None),
            ("Maternal", "Maternal", "SpecialPopulation", None, None),
            ("Pediatric", "Pediatric", "SpecialPopulation", None, None),
            ("PediatricAge0", "Age 0", "Pediatric", None, None),
            ("UnresolvedICDGroup", "Unresolved groups", "Observation", None, None),
            ("ICD_Chapter_1", "Synthetic chapter (A00-B99)", "Observation", "chapter", None),
            ("ICD_Block_A00_A09", "Synthetic block", "ICD_Chapter_1", "block", None),
            ("ICD_A00", "Synthetic A category", "ICD_Block_A00_A09", "code", "A00"),
            ("ICD_A001", "Original ICD & <condition>", "ICD_A00", "code", "A001"),
            ("ICD_A002", "Original second ICD name", "ICD_A00", "code", "A002"),
            ("ICD_B001", "Original ambiguous ICD name", "Observation", "code", "B001"),
            ("ICD_S52", "Synthetic injury category", "Observation", "code", "S52"),
            ("ICD_S52501", "Synthetic official six-character code", "ICD_S52", "code", "S52501"),
            ("ICD_Group_S52501", "Original analytical group label", "ICD_S52", "analytical_group", "S52501"),
            ("ICD_Group_T00000", "Original unresolved group label", "UnresolvedICDGroup", "unresolved_group", "T00000"),
        ]
        for local, label, parent, kind, code in classes:
            element = ET.SubElement(root, f"{{{OWL}}}Class", {f"{{{RDF}}}about": BASE + local})
            label_node = ET.SubElement(element, f"{{{RDFS}}}label")
            label_node.text = label
            label_node.set(f"{{{XML}}}lang", "en")
            if parent:
                ET.SubElement(element, f"{{{RDFS}}}subClassOf", {f"{{{RDF}}}resource": BASE + parent})
            if kind:
                ET.SubElement(element, f"{{{BASE}}}nodeKind").text = kind
            if code:
                ET.SubElement(element, f"{{{BASE}}}ICD_Code").text = code
            if local == "ICD_A00":
                ET.SubElement(element, f"{{{BASE}}}directlyObserved").text = "false"
            if local == "ICD_A001":
                for population in ["Maternal", "Pediatric", "PediatricAge0"]:
                    ET.SubElement(element, f"{{{BASE}}}observedInPopulation", {f"{{{RDF}}}resource": BASE + population})
                ET.SubElement(element, f"{{{BASE}}}curatorNote").text = "Keep custom & original annotation"
            if local == "ICD_Group_S52501":
                ET.SubElement(element, f"{{{BASE}}}originalICDCode").text = "S52501A"
        ET.ElementTree(root).write(self.input, encoding="utf-8", xml_declaration=True)

    def run_annotation(self, **kwargs):
        output = self.folder / ("out-" + str(len(list(self.folder.glob("out-*")))))
        result = annotate(self.input, self.archive, output, release="2026AA", **kwargs)
        root = ET.parse(output / "empo_icd_umls.owl").getroot()
        classes = {c.attrib[f"{{{RDF}}}about"]: c for c in root.findall(f"{{{OWL}}}Class")}
        return output, root, classes, result

    def values(self, element, name, namespace=BASE):
        return {child.text for child in element.findall(f"{{{namespace}}}{name}")}

    def test_unique_preferred_name_preserves_original_label_and_graph(self):
        before = self.input.read_bytes()
        _, root, classes, _ = self.run_annotation()
        selected = classes[BASE + "ICD_A001"]
        self.assertEqual(self.values(selected, "label", RDFS), {PREFERRED})
        original = selected.find(f"{{{BASE}}}icdPreferredName")
        self.assertEqual(original.text, "Original ICD & <condition>")
        self.assertEqual(original.get(f"{{{XML}}}lang"), "en")
        input_classes = {c.attrib[f"{{{RDF}}}about"]: c for c in ET.fromstring(before).findall(f"{{{OWL}}}Class")}
        self.assertEqual(set(classes), set(input_classes))
        for iri, prior in input_classes.items():
            for namespace, name in [(RDFS, "subClassOf"), (BASE, "observedInPopulation"), (BASE, "curatorNote"), (BASE, "nodeKind"), (BASE, "ICD_Code"), (BASE, "originalICDCode"), (BASE, "directlyObserved")]:
                self.assertEqual([(x.text, dict(x.attrib)) for x in prior.findall(f"{{{namespace}}}{name}")], [(x.text, dict(x.attrib)) for x in classes[iri].findall(f"{{{namespace}}}{name}")])
        self.assertEqual(self.input.read_bytes(), before)

    def test_any_official_level_including_unobserved_ancestors_is_eligible(self):
        _, _, classes, report = self.run_annotation()
        for local, cui in [("ICD_A00", "C0000010"), ("ICD_A001", "C0000001"), ("ICD_S52501", "C0000004"), ("ICD_Chapter_1", "C0000011"), ("ICD_Block_A00_A09", "C0000012")]:
            with self.subTest(local=local):
                self.assertEqual(self.values(classes[BASE + local], "umlsCUI"), {cui})
                self.assertEqual(self.values(classes[BASE + local], "umlsMappingStatus"), {"matched"})
        self.assertEqual(self.values(classes[BASE + "ICD_A00"], "directlyObserved"), {"false"})
        self.assertEqual(report["eligible_chapters"], 1)
        self.assertEqual(report["eligible_blocks"], 1)

    def test_single_code_block_and_category_share_exact_anchor_without_merging(self):
        tree = ET.parse(self.input)
        root = tree.getroot()
        for local, kind, parent in [("ICD_Block_C50", "block", "Observation"), ("ICD_C50", "code", "ICD_Block_C50")]:
            element = ET.SubElement(root, f"{{{OWL}}}Class", {f"{{{RDF}}}about": BASE + local})
            ET.SubElement(element, f"{{{RDFS}}}label").text = "Invented single-code " + kind
            ET.SubElement(element, f"{{{RDFS}}}subClassOf", {f"{{{RDF}}}resource": BASE + parent})
            ET.SubElement(element, f"{{{BASE}}}nodeKind").text = kind
            if kind == "code":
                ET.SubElement(element, f"{{{BASE}}}ICD_Code").text = "C50"
        tree.write(self.input, encoding="utf-8", xml_declaration=True)
        self.atoms.append(atom("C0000020", "C50", "Invented shared C50 anchor", "A0000020"))
        self.write_zip()
        _, _, classes, _ = self.run_annotation()
        for local, parent in [("ICD_Block_C50", "Observation"), ("ICD_C50", "ICD_Block_C50")]:
            with self.subTest(local=local):
                element = classes[BASE + local]
                self.assertEqual(self.values(element, "umlsCUI"), {"C0000020"})
                self.assertEqual(self.values(element, "umlsMappingStatus"), {"matched"})
                self.assertEqual({link.get(f"{{{RDF}}}resource") for link in element.findall(f"{{{RDFS}}}subClassOf")}, {BASE + parent})

    def test_terms_are_cui_scoped_filtered_and_xml_escaped(self):
        output, _, classes, _ = self.run_annotation()
        synonyms = self.values(classes[BASE + "ICD_A001"], "Synonyms")
        self.assertIn(SYNONYM, synonyms)
        self.assertNotIn("Excluded non-English atom", synonyms)
        self.assertNotIn("Excluded suppressed atom", synonyms)
        self.assertNotIn("CUI two preferred name", synonyms)
        self.assertIn(b"&amp;", (output / "empo_icd_umls.owl").read_bytes())
        terms = (output / "umls_terms.csv").read_text(encoding="utf-8")
        self.assertIn("A0000002", terms)
        self.assertIn("SNOMEDCT_US", terms)
        self.assertIn(PREFERRED, terms)

    def test_ambiguous_code_does_not_union_terms_even_if_cui_used_elsewhere(self):
        _, _, classes, _ = self.run_annotation()
        ambiguous = classes[BASE + "ICD_B001"]
        self.assertEqual(self.values(ambiguous, "label", RDFS), {"Original ambiguous ICD name"})
        self.assertEqual(self.values(ambiguous, "umlsCandidateCUI"), {"C0000002", "C0000003"})
        self.assertEqual(self.values(ambiguous, "umlsCUI"), set())
        self.assertEqual(self.values(ambiguous, "Synonyms"), set())
        self.assertNotEqual(self.values(classes[BASE + "ICD_A002"], "Synonyms"), set())

    def test_grouped_and_unresolved_nodes_do_not_get_synonym_pools(self):
        _, _, classes, _ = self.run_annotation()
        for local, label in [("ICD_Group_S52501", "Original analytical group label"), ("ICD_Group_T00000", "Original unresolved group label")]:
            with self.subTest(local=local):
                cls = classes[BASE + local]
                self.assertEqual(self.values(cls, "label", RDFS), {label})
                self.assertEqual(self.values(cls, "Synonyms"), set())
                self.assertEqual(self.values(cls, "umlsCandidateCUI"), set())
                self.assertEqual(self.values(cls, "umlsCUI"), set())

    def test_keep_icd_labels_mode(self):
        _, _, classes, _ = self.run_annotation(keep_icd_labels=True)
        selected = classes[BASE + "ICD_A001"]
        self.assertEqual(self.values(selected, "label", RDFS), {"Original ICD & <condition>"})
        self.assertTrue(any(child.text == PREFERRED for child in selected))

    def test_conflicting_flag_preferred_strings_keep_original_label(self):
        self.atoms.append(atom("C0000001", "D002", "Another genuinely flag-preferred string", "A0000099", SAB="MSH", TS="P", STT="PF", ISPREF="Y"))
        self.write_zip()
        _, _, classes, _ = self.run_annotation()
        selected = classes[BASE + "ICD_A001"]
        self.assertEqual(self.values(selected, "label", RDFS), {"Original ICD & <condition>"})
        self.assertIn(SYNONYM, self.values(selected, "Synonyms"))

    def test_already_owned_annotation_is_rejected_without_modification(self):
        output, _, _, _ = self.run_annotation()
        annotated = output / "empo_icd_umls.owl"
        digest = hashlib.sha256(annotated.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            annotate(annotated, self.archive, self.folder / "second", release="2026AA")
        self.assertEqual(hashlib.sha256(annotated.read_bytes()).hexdigest(), digest)

    def test_failure_leaves_input_unchanged_and_no_published_artifact(self):
        before = self.input.read_bytes()
        self.write_zip(extra="malformed|last|row|\n")
        output = self.folder / "failed"
        with self.assertRaises(ValueError):
            annotate(self.input, self.archive, output, release="2026AA")
        self.assertEqual(self.input.read_bytes(), before)
        for filename in ["empo_icd_umls.owl", "umls_code_matches.csv", "umls_terms.csv", "umls_annotation_report.json"]:
            self.assertFalse((output / filename).exists(), filename)

    def test_partial_serialization_failure_never_publishes_staged_files(self):
        before = self.input.read_bytes()
        output = self.folder / "write-failed"

        def fail_after_partial_write(tree, path, *args, **kwargs):
            Path(path).write_text("incomplete staged XML", encoding="utf-8")
            raise OSError("Synthetic output failure")

        with patch("annotate_umls.ET.ElementTree.write", fail_after_partial_write):
            with self.assertRaisesRegex(OSError, "Synthetic output failure"):
                annotate(self.input, self.archive, output, release="2026AA")
        self.assertEqual(self.input.read_bytes(), before)
        self.assertEqual(list(output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
