"""Boundary and semantic-preservation checks for the MeSH descriptor reader."""
import gzip
import hashlib
from pathlib import Path
import tempfile
import unittest
import warnings
import zipfile

from mesh_reader import read_mesh


def record(identifier="D000001", trees=("C01",), concepts=None):
    if concepts is None:
        concepts = f'''<Concept PreferredConceptYN="Y"><ConceptUI>M{identifier[1:]}</ConceptUI>
        <ConceptName><String>Concept {identifier}</String></ConceptName>
        <TermList><Term><String>Term {identifier}</String></Term></TermList></Concept>'''
    tree_elements = "".join(f"<TreeNumber>{tree}</TreeNumber>" for tree in trees)
    return f'''<DescriptorRecord><DescriptorUI>{identifier}</DescriptorUI>
    <DescriptorName><String>Heading {identifier}</String></DescriptorName>
    <TreeNumberList>{tree_elements}</TreeNumberList><ConceptList>{concepts}</ConceptList>
    </DescriptorRecord>'''


def document(*records, year="2025"):
    return (f'''<?xml version="1.0"?>
    <!DOCTYPE DescriptorRecordSet SYSTEM "https://example.invalid/nlmdescriptorrecordset_{year}0101.dtd">
    <DescriptorRecordSet>{"".join(records)}</DescriptorRecordSet>''').encode("utf-8")


class MeSHReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def write(self, content, name="desc.xml"):
        path = self.directory / name
        path.write_bytes(content)
        return path

    def test_polyhierarchy_preserves_all_tree_positions_and_broader_edges(self):
        source = document(record(trees=("C01", "C02")),
                          record("D000002", ("C01.100", "C02.200")),
                          record("D000003", ()))
        data = read_mesh(self.write(source))
        self.assertEqual(data["parents"]["D000002"], {"D000001"})
        self.assertEqual(data["descriptors"]["D000002"]["tree_numbers"], ["C01.100", "C02.200"])
        self.assertEqual(data["root_trees"], ["C01", "C02"])
        self.assertEqual(len(data["tree_parents"]), 2)
        self.assertEqual(data["counts"]["descriptors_without_trees"], 1)
        self.assertEqual(data["provenance"]["file_sha256"], hashlib.sha256(source).hexdigest())

    def test_narrower_concept_terms_and_scope_are_kept_separately(self):
        concepts = '''<Concept PreferredConceptYN="Y"><ConceptUI>M000001</ConceptUI>
          <ConceptName><String>Broad concept</String></ConceptName>
          <ScopeNote>Broad scope &amp; evidence.</ScopeNote>
          <ConceptRelationList><ConceptRelation RelationName="NRW"><Concept1UI>M000001</Concept1UI>
          <Concept2UI>M000002</Concept2UI></ConceptRelation></ConceptRelationList>
          <TermList><Term><String>Broad term</String></Term></TermList></Concept>
          <Concept PreferredConceptYN="N"><ConceptUI>M000002</ConceptUI>
          <ConceptName><String>Narrow concept</String></ConceptName><ScopeNote>Narrow scope.</ScopeNote>
          <TermList><Term><String>Narrow term</String></Term></TermList></Concept>'''
        item = read_mesh(self.write(document(record(concepts=concepts))))["descriptors"]["D000001"]
        self.assertEqual(item["concepts"][0]["terms"], ["Broad term"])
        self.assertEqual(item["concepts"][1]["terms"], ["Narrow term"])
        self.assertFalse(item["scope_notes"][1]["preferred"])
        self.assertEqual(item["scope_notes"][0]["text"], "Broad scope & evidence.")
        self.assertEqual(item["concept_relations"][0]["relation_name"], "NRW")
        self.assertNotIn("synonyms", item)

    def test_zip_metadata_ignored_and_explicit_ambiguous_member_required(self):
        path = self.directory / "mesh.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("META/desc.xml", document(record()))
            archive.writestr("__MACOSX/._desc.xml", b"not XML")
        self.assertEqual(read_mesh(path)["provenance"]["member"], "META/desc.xml")
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("second.xml", document(record()))
        with self.assertRaisesRegex(ValueError, "unambiguous"):
            read_mesh(path)
        self.assertEqual(read_mesh(path, member="META/desc.xml")["counts"]["descriptors"], 1)

    def test_duplicate_zip_member_rejected_even_with_explicit_selection(self):
        path = self.directory / "duplicate.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("desc.xml", document(record()))
                archive.writestr("desc.xml", document(record()))
        with self.assertRaisesRegex(ValueError, "duplicat"):
            read_mesh(path, member="desc.xml")

    def test_gzip_supported_without_extraction(self):
        path = self.write(gzip.compress(document(record())), "desc.xml.gz")
        self.assertEqual(read_mesh(path)["counts"]["descriptors"], 1)
        with self.assertRaisesRegex(ValueError, "only for ZIP"):
            read_mesh(path, member="desc.xml")

    def test_year_mismatch_and_missing_dtd_fail(self):
        path = self.write(document(record(), year="2024"))
        with self.assertRaisesRegex(ValueError, "does not match"):
            read_mesh(path)
        self.assertEqual(read_mesh(path, expected_year="2024")["provenance"]["declared_year"], "2024")
        no_dtd = f"<DescriptorRecordSet>{record()}</DescriptorRecordSet>".encode()
        with self.assertRaisesRegex(ValueError, "does not match"):
            read_mesh(self.write(no_dtd))
        self.assertIsNone(read_mesh(self.write(no_dtd), expected_year=None)["provenance"]["declared_year"])

    def test_missing_parents_fail_including_category_roots(self):
        for trees in [("C01.100",), ("C01", "C01.100.200")]:
            with self.subTest(trees=trees), self.assertRaisesRegex(ValueError, "missing parent"):
                read_mesh(self.write(document(record(trees=trees))))

    def test_duplicate_ids_and_shared_tree_positions_fail(self):
        for records in [(record(), record()), (record(), record("D000002"))]:
            with self.subTest(records=records), self.assertRaises(ValueError):
                read_mesh(self.write(document(*records)))

    def test_invalid_tree_and_descriptor_cycles_fail(self):
        with self.assertRaisesRegex(ValueError, "invalid MeSH tree"):
            read_mesh(self.write(document(record(trees=("C01.1",)))))
        with self.assertRaisesRegex(ValueError, "cycle"):
            read_mesh(self.write(document(record(trees=("C01", "C02.100")),
                                          record("D000002", ("C02", "C01.100")))))

    def test_internal_entity_declarations_and_malformed_xml_fail(self):
        content = b'''<!DOCTYPE DescriptorRecordSet [<!ENTITY example "EXPANDED">]>
          <DescriptorRecordSet></DescriptorRecordSet>'''
        with self.assertRaisesRegex(ValueError, "entity declarations"):
            read_mesh(self.write(content), expected_year=None)
        with self.assertRaisesRegex(ValueError, "Invalid MeSH XML"):
            read_mesh(self.write(document(record())[:-10]))


if __name__ == "__main__":
    unittest.main()
