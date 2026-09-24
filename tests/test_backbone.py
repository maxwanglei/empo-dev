"""Focused backbone checks with tiny invented references, not clinical data."""

import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from build_ontology import (
    AGE_TERMS, DEFAULT_BASE, OWL, RDF, RDFS, build, build_model, check_dag,
    group_code, iri_for, normalize_code, read_observations, write_owl,
)


def node(label, parents=(), kind="code", materialize=True):
    return {
        "label": label, "parents": list(parents), "kind": kind,
        "code": label, "source_releases": ["synthetic-2021"],
        "selected_release": "synthetic-2021", "materialize": materialize,
    }


def reference():
    # Labels and relationships are invented to test graph behavior. In
    # particular, S12X1 is deliberately not a prefix of S12XXXA/S12XXXD.
    return {
        "chapter:TEST": node("Synthetic chapter", kind="chapter"),
        "block:S00-T99": node("Synthetic block", ["chapter:TEST"], "block"),
        "code:S12": node("Synthetic category", ["block:S00-T99"]),
        "code:S12X1": node("Explicit shared ancestor", ["code:S12"]),
        "code:S12XXXA": node("Synthetic member A", ["code:S12X1"]),
        "code:S12XXXD": node("Synthetic member D", ["code:S12X1"]),
        "code:O99": node("Synthetic maternal category", ["chapter:TEST"]),
    }


class BackboneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def csv_input(self, pairs):
        path = self.folder / "input.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["ICD10", "population"])
            writer.writerows(pairs)
        return path

    def owl(self, observations, refs=None):
        nodes, groups, _ = build_model(observations, refs or reference())
        path = self.folder / "backbone.owl"
        write_owl(path, DEFAULT_BASE, nodes, groups, "synthetic.csv", "input-hash", "reference-hash")
        root = ET.parse(path).getroot()
        classes = {c.attrib[f"{{{RDF}}}about"]: c for c in root.findall(f"{{{OWL}}}Class")}
        return nodes, groups, root, classes

    def test_normalization_and_seven_to_six_keep_x(self):
        self.assertEqual(normalize_code(" s12.xxxa "), "S12XXXA")
        self.assertEqual(group_code(normalize_code("S12.XXXA")), "S12XXX")
        self.assertEqual(group_code("S12XXX"), "S12XXX")
        self.assertEqual(group_code("O0901"), "O0901")
        self.assertEqual(group_code("A00"), "A00")
        self.assertEqual(normalize_code("o09.01"), "O0901")

    def test_invalid_decimal_position_rejected(self):
        for code in ["S1.2XXXA", "S12.XX.XA", "S12XXXAB", "001.1"]:
            with self.subTest(code=code), self.assertRaises(ValueError):
                normalize_code(code)

    def test_nodx_exclusion_duplicates_and_population_overlap(self):
        path = self.csv_input([
            ("NoDx", "Maternal"), (" nodx ", "0"),
            ("s12.xxxa", "Maternal"), ("S12XXXA", "Maternal"),
            ("S12XXXA", "0"), ("S12XXXA", "1-12"),
            ("S12XXXD", "13-17"),
        ])
        pairs, report = read_observations(path)
        self.assertEqual(pairs, {("S12XXXA", "Maternal"), ("S12XXXA", "0"), ("S12XXXA", "1-12"), ("S12XXXD", "13-17")})
        self.assertEqual(report["input_rows"], 7)
        self.assertEqual(report["excluded_NoDx_rows"], 2)
        self.assertEqual(report["duplicate_rows_removed_after_NoDx"], 1)
        self.assertEqual(report["unique_original_code_population_pairs"], 4)

    def test_common_parent_comes_from_edges_not_string_prefix(self):
        nodes, groups, unresolved = build_model({("S12XXXA", "Maternal"), ("S12XXXD", "0")}, reference())
        group = groups["S12XXX"]
        self.assertEqual(group["kind"], "analytical_group")
        self.assertEqual(nodes[group["key"]]["parents"], ["code:S12X1"])
        self.assertEqual(unresolved, [])
        self.assertNotIn("code:S12XXX", nodes)

    def test_existing_group_code_reused_only_when_ancestor_of_members(self):
        refs = reference()
        refs["code:S12XXX"] = node("Official grouping node", ["code:S12"])
        for member in ["code:S12XXXA", "code:S12XXXD"]:
            refs[member]["parents"] = ["code:S12XXX"]
        nodes, groups, _ = build_model({("S12XXXA", "Maternal"), ("S12XXXD", "0")}, refs)
        self.assertEqual(groups["S12XXX"]["key"], "code:S12XXX")
        self.assertEqual(groups["S12XXX"]["kind"], "official")
        self.assertIn("code:S12XXX", nodes)

    def test_existing_group_code_with_wrong_parentage_is_not_reused(self):
        refs = reference()
        refs["code:S12XXX"] = node("Unrelated official code", ["code:O99"])
        nodes, groups, _ = build_model({("S12XXXA", "Maternal"), ("S12XXXD", "0")}, refs)
        self.assertEqual(groups["S12XXX"]["key"], "group:S12XXX")
        self.assertEqual(nodes["group:S12XXX"]["parents"], ["code:S12X1"])

    def test_official_prefix_without_any_matched_members_is_not_reused(self):
        refs = reference()
        refs["code:T00XXX"] = node("Existing official prefix", ["chapter:TEST"])
        nodes, groups, unresolved = build_model({("T00XXXA", "Maternal")}, refs)
        self.assertEqual(groups["T00XXX"]["key"], "group:T00XXX")
        self.assertEqual(groups["T00XXX"]["kind"], "unresolved_group")
        self.assertEqual(nodes["group:T00XXX"]["parents"], [])
        self.assertEqual(unresolved, ["T00XXXA"])

    def test_mixed_matched_and_unmatched_members_are_not_called_official(self):
        refs = reference()
        refs["code:S12XXX"] = node("Official grouping node", ["code:S12"])
        refs["code:S12XXXA"]["parents"] = ["code:S12XXX"]
        _, groups, _ = build_model({("S12XXXA", "Maternal"), ("S12XXXS", "0")}, refs)
        self.assertEqual(groups["S12XXX"]["key"], "group:S12XXX")
        self.assertEqual(groups["S12XXX"]["kind"], "analytical_group")
        self.assertEqual(groups["S12XXX"]["unresolved_members"], ["S12XXXS"])

    def test_missing_reference_members_are_preserved_and_flagged(self):
        nodes, groups, unresolved = build_model({("S12XXXA", "Maternal"), ("S12XXXS", "1-12")}, reference())
        self.assertEqual(groups["S12XXX"]["members"], ["S12XXXA", "S12XXXS"])
        self.assertEqual(groups["S12XXX"]["unresolved_members"], ["S12XXXS"])
        self.assertEqual(groups["S12XXX"]["memberships"], ["1-12", "Maternal"])
        self.assertEqual(unresolved, ["S12XXXS"])
        self.assertIn("group:S12XXX", nodes)

    def test_missing_parent_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "Missing parent"):
            check_dag({"code:A00": node("Broken node", ["code:MISSING"])})

    def test_disconnected_cycle_is_an_error(self):
        refs = reference()
        refs["code:A00"] = node("Cycle one", ["code:A01"])
        refs["code:A01"] = node("Cycle two", ["code:A00"])
        with self.assertRaisesRegex(ValueError, "cycle"):
            build_model({("S12XXXA", "Maternal")}, refs)

    def test_owl_population_graph_is_separate_and_overlap_uses_iris(self):
        observations = {("S12XXXA", population) for population in ["Maternal", "0", "1-12", "13-17"]}
        nodes, groups, _, classes = self.owl(observations)
        group_class = classes[iri_for(groups["S12XXX"]["key"], DEFAULT_BASE)]
        targets = {e.attrib[f"{{{RDF}}}resource"] for e in group_class.findall(f"{{{DEFAULT_BASE}}}observedInPopulation")}
        population_iris = {DEFAULT_BASE + name for name in ["Maternal", "Pediatric", *AGE_TERMS.values()]}
        self.assertEqual(targets, population_iris)
        for key in nodes:
            cls = classes[iri_for(key, DEFAULT_BASE)]
            parents = {e.attrib[f"{{{RDF}}}resource"] for e in cls.findall(f"{{{RDFS}}}subClassOf")}
            self.assertFalse(parents & population_iris)
        for name in ["Maternal", "Pediatric"]:
            parent = classes[DEFAULT_BASE + name].find(f"{{{RDFS}}}subClassOf")
            self.assertEqual(parent.attrib[f"{{{RDF}}}resource"], DEFAULT_BASE + "SpecialPopulation")
        for name in AGE_TERMS.values():
            parent = classes[DEFAULT_BASE + name].find(f"{{{RDFS}}}subClassOf")
            self.assertEqual(parent.attrib[f"{{{RDF}}}resource"], DEFAULT_BASE + "Pediatric")

    def test_added_ancestors_are_not_marked_observed(self):
        nodes, groups, _, classes = self.owl({("S12XXXA", "Maternal"), ("S12XXXD", "0")})
        observed_key = groups["S12XXX"]["key"]
        for key in nodes:
            cls = classes[iri_for(key, DEFAULT_BASE)]
            observed = cls.find(f"{{{DEFAULT_BASE}}}directlyObserved")
            self.assertEqual(observed.text, "true" if key == observed_key else "false")
            if key != observed_key:
                self.assertEqual(cls.findall(f"{{{DEFAULT_BASE}}}observedInPopulation"), [])

    def test_mapping_output_preserves_original_code_population_pairs(self):
        path = self.csv_input([("S12XXXA", "Maternal"), ("S12XXXD", "0"), ("S12XXXD", "13-17"), ("S12XXXA", "Maternal")])
        manifest = self.folder / "reference.json"
        manifest.write_text(json.dumps({"test": "invented reference only"}), encoding="utf-8")
        out = self.folder / "out"
        with patch("build_ontology.parse_references", return_value=reference()):
            report = build(path, manifest, out)
        with (out / "code_population_mapping.csv").open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual({(r["original_icd"], r["source_population"]) for r in rows}, {("S12XXXA", "Maternal"), ("S12XXXD", "0"), ("S12XXXD", "13-17")})
        self.assertEqual({r["grouped_icd"] for r in rows}, {"S12XXX"})
        self.assertEqual({(r["population"], r["age_group"]) for r in rows}, {("Maternal", ""), ("Pediatric", "0"), ("Pediatric", "13-17")})
        self.assertEqual(report["groups_in_both_populations"], 1)
        self.assertEqual(report["observed_code_groups"], 1)
        self.assertTrue(report["validation"]["rdf_xml_well_formed"])


if __name__ == "__main__":
    unittest.main()
