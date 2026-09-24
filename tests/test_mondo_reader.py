"""MONDO reader checks using invented, independently assembled RDF/XML."""

import gzip
import hashlib
from pathlib import Path
import tempfile
import unittest
import warnings
import xml.etree.ElementTree as ET
import zipfile

from mondo_reader import read_mondo


RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"
OBO = "http://purl.obolibrary.org/obo/"
OIO = "http://www.geneontology.org/formats/oboInOwl#"
MONDO = OBO + "MONDO_"
HUMAN = MONDO + "0700096"
SKOS = "http://www.w3.org/2004/02/skos/core#"
XML = "http://www.w3.org/XML/1998/namespace"


def disease(identifier, parents=(), extra="", label=None):
    parent_xml = "".join(f'<rdfs:subClassOf rdf:resource="{parent}"/>' for parent in parents)
    return f'''<owl:Class rdf:about="{MONDO + identifier}">
      <rdfs:label xml:lang="en">{label or 'Invented disease ' + identifier}</rdfs:label>
      {parent_xml}{extra}</owl:Class>'''


def document(*records, extras="", ontology=True):
    declaration = '''<owl:Ontology rdf:about="http://purl.obolibrary.org/obo/mondo.owl">
      <owl:versionIRI rdf:resource="http://purl.obolibrary.org/obo/mondo/releases/2026-09-01/mondo.owl"/>
      <owl:imports rdf:resource="https://example.invalid/must-not-fetch.owl"/>
      </owl:Ontology>''' if ontology else ""
    return f'''<?xml version="1.0" encoding="UTF-8"?>
    <rdf:RDF xmlns:rdf="{RDF}" xmlns:rdfs="{RDFS}" xmlns:owl="{OWL}"
      xmlns:obo="{OBO}" xmlns:oboInOwl="{OIO}" xmlns:skos="{SKOS}"
      xmlns:xsd="http://www.w3.org/2001/XMLSchema#">
      {declaration}{''.join(records)}{extras}</rdf:RDF>'''.encode()


def sample_document():
    """Includes two human parents, external boundaries, and mapping qualifiers."""
    root = disease("0700096", (MONDO + "0000001",), label="human disease")
    left = disease("9000001", (HUMAN,), '''
      <oboInOwl:hasDbXref>ICD10CM:A00.1</oboInOwl:hasDbXref>
      <oboInOwl:hasDbXref>ICD10:A00.2</oboInOwl:hasDbXref>
      <oboInOwl:hasDbXref>ICD9:001.1</oboInOwl:hasDbXref>
      <oboInOwl:hasDbXref>UMLS:C1000001</oboInOwl:hasDbXref>
      <oboInOwl:hasDbXref>MESH:D000001</oboInOwl:hasDbXref>''')
    right = disease("9000002", (HUMAN,))
    child = disease("9000003", (MONDO + "9000001", MONDO + "9000002", MONDO + "9000099"), '''
      <obo:IAO_0000115 xml:lang="en">Invented definition &amp; provenance.</obo:IAO_0000115>
      <oboInOwl:hasExactSynonym xml:lang="en">Exact invented name</oboInOwl:hasExactSynonym>
      <oboInOwl:hasBroadSynonym>Broad invented name</oboInOwl:hasBroadSynonym>
      <oboInOwl:hasNarrowSynonym>Narrow invented name</oboInOwl:hasNarrowSynonym>
      <oboInOwl:hasRelatedSynonym>Related invented name</oboInOwl:hasRelatedSynonym>
      <skos:exactMatch rdf:resource="http://identifiers.org/icd10cm/A00.2"/>
      <rdfs:subClassOf><owl:Restriction>
        <owl:onProperty rdf:resource="http://purl.obolibrary.org/obo/RO_0000052"/>
        <owl:someValuesFrom rdf:resource="http://purl.obolibrary.org/obo/NCBITaxon_9606"/>
      </owl:Restriction></rdfs:subClassOf>''')
    deprecated = disease("9000004", (HUMAN,), '''
      <owl:deprecated rdf:datatype="http://www.w3.org/2001/XMLSchema#boolean">true</owl:deprecated>
      <oboInOwl:hasDbXref>ICD10CM:B00</oboInOwl:hasDbXref>''')
    outside = disease("9000099", (MONDO + "0000001",), '<oboInOwl:hasDbXref>ICD10CM:C00</oboInOwl:hasDbXref>')
    extras = f'''<owl:AnnotationProperty rdf:about="{OIO}hasExactSynonym">
      <rdfs:label>exact synonym</rdfs:label></owl:AnnotationProperty>
      <owl:Axiom><owl:annotatedSource rdf:resource="{MONDO}9000001"/>
        <owl:annotatedProperty rdf:resource="{OIO}hasDbXref"/>
        <owl:annotatedTarget>ICD10CM:A00.1</owl:annotatedTarget>
        <oboInOwl:source rdf:resource="http://purl.obolibrary.org/obo/mondo#equivalentTo"/>
        <oboInOwl:source>Invented curator evidence</oboInOwl:source>
      </owl:Axiom>
      <owl:Axiom><owl:annotatedSource rdf:resource="{MONDO}9000003"/>
        <owl:annotatedProperty rdf:resource="{OIO}hasExactSynonym"/>
        <owl:annotatedTarget xml:lang="en">Exact invented name</owl:annotatedTarget>
        <oboInOwl:source>Invented synonym citation</oboInOwl:source>
      </owl:Axiom>'''
    return document(disease("0000001"), root, left, right, child, deprecated, outside, extras=extras)


class MondoReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def write(self, data, name="mondo.owl"):
        path = self.folder / name
        path.write_bytes(data)
        return path

    def test_only_active_human_branch_and_all_native_internal_parents(self):
        data = read_mondo(self.write(sample_document()))
        expected = {HUMAN, *(MONDO + n for n in ("9000001", "9000002", "9000003"))}
        self.assertEqual(set(data["records"]), expected)
        self.assertEqual(data["parents"][MONDO + "9000003"], {MONDO + "9000001", MONDO + "9000002"})
        self.assertEqual(data["parents"][HUMAN], set())
        self.assertIn(MONDO + "9000099", str(data["boundary_parents"]))
        self.assertIn(MONDO + "0000001", str(data["boundary_parents"]))
        self.assertTrue(data["omitted_axioms"], "Complex restrictions must be audited rather than silently dropped")

    def test_typed_synonyms_definition_and_source_axioms_preserved(self):
        data = read_mondo(self.write(sample_document()))
        child = data["records"][MONDO + "9000003"]
        annotations = [ET.fromstring(value) for value in child["annotations"]]
        for kind, text in [("Exact", "Exact invented name"), ("Broad", "Broad invented name"),
                           ("Narrow", "Narrow invented name"), ("Related", "Related invented name")]:
            self.assertEqual([a.text for a in annotations if a.tag == f"{{{OIO}}}has{kind}Synonym"], [text])
        definition = next(a for a in annotations if a.tag == f"{{{OBO}}}IAO_0000115")
        self.assertEqual(definition.text, "Invented definition & provenance.")
        self.assertEqual(definition.get(f"{{{XML}}}lang"), "en")
        self.assertIn("Invented synonym citation", "".join(child["source_axioms"]))
        mapped = data["records"][MONDO + "9000001"]
        self.assertIn("Invented curator evidence", "".join(mapped["source_axioms"]))
        self.assertIn("ICD10CM:A00.1", str(mapped["icd_mappings"]))
        self.assertIn("equivalentTo", str(mapped["icd_mappings"]))
        self.assertIn("hasExactSynonym", "".join(data["annotation_properties"]))

    def test_provenance_hash_and_version_and_does_not_fetch_import(self):
        source = sample_document()
        data = read_mondo(self.write(source))
        self.assertIn(hashlib.sha256(source).hexdigest(), str(data["provenance"]))
        self.assertEqual(data["provenance"]["version_iri"], "http://purl.obolibrary.org/obo/mondo/releases/2026-09-01/mondo.owl")
        self.assertEqual(data["provenance"]["ontology_iri"], "http://purl.obolibrary.org/obo/mondo.owl")

    def test_zip_metadata_ignored_and_explicit_member_disambiguates(self):
        path = self.folder / "mondo.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("data/mondo.owl", sample_document())
            archive.writestr("__MACOSX/._mondo.owl", b"not XML")
        self.assertEqual(read_mondo(path)["provenance"]["member"], "data/mondo.owl")
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("other.owl", sample_document())
        with self.assertRaises(ValueError):
            read_mondo(path)
        self.assertEqual(len(read_mondo(path, member="data/mondo.owl")["records"]), 4)

    def test_duplicate_zip_member_is_rejected_even_if_named(self):
        path = self.folder / "duplicate.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("mondo.owl", sample_document())
                archive.writestr("mondo.owl", sample_document())
        with self.assertRaises(ValueError):
            read_mondo(path, member="mondo.owl")

    def test_gzip_and_plain_input_yield_same_hierarchy(self):
        plain = read_mondo(self.write(sample_document()))
        zipped = read_mondo(self.write(gzip.compress(sample_document()), "mondo.owl.gz"))
        self.assertEqual(plain["parents"], zipped["parents"])
        with self.assertRaises(ValueError):
            read_mondo(self.folder / "mondo.owl.gz", member="mondo.owl")

    def test_missing_deprecated_or_duplicate_human_root_fails(self):
        cases = [document(disease("9000001")),
                 document(disease("0700096", extra='<owl:deprecated rdf:datatype="http://www.w3.org/2001/XMLSchema#boolean">true</owl:deprecated>')),
                 document(disease("0700096"), disease("0700096")),
                 document(disease("0700096"), ontology=False)]
        for source in cases:
            with self.subTest(source=source), self.assertRaises(ValueError):
                read_mondo(self.write(source))

    def test_cycle_within_human_branch_fails(self):
        source = document(disease("0700096"),
                          disease("9000001", (HUMAN, MONDO + "9000002")),
                          disease("9000002", (MONDO + "9000001",)))
        with self.assertRaisesRegex(ValueError, "[Cc]ycle"):
            read_mondo(self.write(source))

    def test_entities_and_malformed_xml_are_rejected(self):
        source = sample_document().replace(b'?>', b'?>\n<!DOCTYPE rdf:RDF [<!ENTITY malicious "replacement">]>', 1)
        with self.assertRaises(ValueError):
            read_mondo(self.write(source))
        with self.assertRaises(ValueError):
            read_mondo(self.write(sample_document()[:-40]))

    def test_oversized_prolog_cannot_hide_entity_declaration(self):
        hidden_declaration = (b"?>\n<!--" + b"padding " * 10000 + b"-->\n"
                              b'<!DOCTYPE rdf:RDF [<!ENTITY malicious "replacement">]>')
        source = sample_document().replace(b"?>", hidden_declaration, 1)
        with self.assertRaises(ValueError):
            read_mondo(self.write(source))


if __name__ == "__main__":
    unittest.main()
