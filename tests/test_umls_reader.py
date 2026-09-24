"""UMLS reader validation using invented MRCONSO atoms only."""

from pathlib import Path
import tempfile
import unittest
import warnings
import zipfile

from umls_reader import (
    MRConsoSource, collect_anchors, is_preferred_atom,
    iter_scoped_atoms, normalize_icd_code, preferred_strings,
)


FIELDS = "CUI LAT TS LUI STT SUI ISPREF AUI SAUI SCUI SDUI SAB TTY CODE STR SRL SUPPRESS CVF".split()


def atom(cui="C0000001", code="A00.1", text="Invented condition", **changes):
    result = dict(zip(FIELDS, [cui, "ENG", "P", "L0000001", "PF", "S0000001", "Y", "A0000001", "", "", "", "ICD10CM", "PT", code, text, "0", "N", ""]))
    result.update(changes)
    return result


def rrf(*atoms):
    return "".join("|".join(a[field] for field in FIELDS) + "|\n" for a in atoms)


class UMLSReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def source(self, atoms, name="release/META/MRCONSO.RRF"):
        path = self.folder / "synthetic-mrconso.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(name, rrf(*atoms))
        return MRConsoSource(path, release="2026AA")

    def test_exact_source_code_and_eligibility_filter_anchor_matching(self):
        source = self.source([
            atom(code="S52.501A"),
            atom(cui="C0000002", code="S52.501A", SAB="ICD10"),
            atom(cui="C0000003", code="S52.501"),
            atom(cui="C0000004", code="S52.501A", LAT="SPA"),
            atom(cui="C0000005", code="S52.501A", SUPPRESS="O"),
            atom(cui="C0000006", code="S52.501A", SUPPRESS="Y"),
            atom(cui="C0000007", code="S52.501A", SUPPRESS="E"),
        ])
        result = collect_anchors(source, {"S52501A", "O0901"})
        record = result["by_code"]["S52501A"]
        self.assertEqual(record["status"], "unambiguous")
        self.assertEqual(record["cuis"], ["C0000001"])
        self.assertEqual(result["selected_cuis"], {"C0000001"})
        self.assertEqual(result["by_code"]["O0901"]["status"], "unmapped")

    def test_two_cuis_for_one_code_remain_ambiguous(self):
        source = self.source([atom(), atom(cui="C0000002", AUI="A0000002")])
        result = collect_anchors(source, {"A001"})
        record = result["by_code"]["A001"]
        self.assertEqual(record["status"], "ambiguous")
        self.assertEqual(record["cuis"], ["C0000001", "C0000002"])
        self.assertEqual(result["selected_cuis"], set())

    def test_duplicate_atoms_for_one_cui_are_not_mapping_ambiguity(self):
        source = self.source([atom(), atom(code="A001", AUI="A0000002", text="Alternate invented atom")])
        result = collect_anchors(source, {"A001"})
        self.assertEqual(result["by_code"]["A001"]["status"], "unambiguous")
        self.assertEqual(result["selected_cuis"], {"C0000001"})

    def test_range_identifiers_are_exact_source_code_matches(self):
        source = self.source([
            atom(cui="C0000001", code="A00-B99"),
            atom(cui="C0000002", code="A00-A09"),
            atom(cui="C0000003", code="A00"),
            atom(cui="C0000004", code="A00-B99", SAB="ICD10"),
        ])
        result = collect_anchors(source, {"A00-B99", "A00-A09", "B00-B09"})
        self.assertEqual(result["by_code"]["A00-B99"]["cuis"], ["C0000001"])
        self.assertEqual(result["by_code"]["A00-A09"]["cuis"], ["C0000002"])
        self.assertEqual(result["by_code"]["B00-B09"]["status"], "unmapped")
        self.assertEqual(result["selected_cuis"], {"C0000001", "C0000002"})

    def test_range_normalization_preserves_hyphen_and_requires_full_endpoints(self):
        self.assertEqual(normalize_icd_code(" a00.0-a09.9 "), "A000-A099")
        for value in ["Chapter 1", "A00-", "-B99", "A00-B99-C99", "A00–B99"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_icd_code(value)

    def test_pass_two_is_scoped_by_cui_language_and_suppression(self):
        source = self.source([
            atom(), atom(SAB="SNOMEDCT_US", CODE="999", STR="Invented alternate & <α>", AUI="A0000002"),
            atom(LAT="FRE", AUI="A0000003"), atom(SUPPRESS="Y", AUI="A0000004"),
            atom(cui="C9999999", AUI="A0000005"),
        ])
        rows = list(iter_scoped_atoms(source, {"C0000001"}))
        self.assertEqual({a["AUI"] for a in rows}, {"A0000001", "A0000002"})
        self.assertEqual({a["SAB"] for a in rows}, {"ICD10CM", "SNOMEDCT_US"})
        self.assertIn("Invented alternate & <α>", {a["STR"] for a in rows})

    def test_preferred_atom_requires_all_three_flags(self):
        self.assertTrue(is_preferred_atom(atom()))
        for changes in [{"TS": "S"}, {"STT": "VC"}, {"ISPREF": "N"}, {"LAT": "SPA"}, {"SUPPRESS": "Y"}]:
            with self.subTest(changes=changes):
                self.assertFalse(is_preferred_atom(atom(**changes)))
        self.assertEqual(preferred_strings([atom(text="Zeta & <α>"), atom(text="Alpha"), atom(text="Zeta & <α>", AUI="A0000002"), atom(text="False preferred", TS="S")]), ["Alpha", "Zeta & <α>"])

    def test_nested_archive_layout_and_row_provenance(self):
        source = self.source([atom()], name="nested/META/mrconso.rrf")
        row = next(source.iter_atoms())
        self.assertEqual(row["_member"], "nested/META/mrconso.rrf")
        self.assertEqual(row["_row_number"], 1)
        self.assertEqual(row["CUI"], "C0000001")

    def test_multiple_archive_members_require_explicit_choice(self):
        path = self.folder / "ambiguous.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("one/MRCONSO.RRF", rrf(atom()))
            archive.writestr("two/mrconso.rrf", rrf(atom(cui="C0000002")))
        with self.assertRaises(ValueError):
            list(MRConsoSource(path).iter_atoms())
        selected = list(MRConsoSource(path, member="two/mrconso.rrf").iter_atoms())
        self.assertEqual([r["CUI"] for r in selected], ["C0000002"])

    def test_duplicate_identical_member_names_are_rejected(self):
        path = self.folder / "duplicate.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("MRCONSO.RRF", rrf(atom()))
                archive.writestr("MRCONSO.RRF", rrf(atom(cui="C0000002")))
        with self.assertRaises(ValueError):
            list(MRConsoSource(path, member="MRCONSO.RRF").iter_atoms())

    def test_malformed_row_fails_with_row_context(self):
        path = self.folder / "malformed.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("MRCONSO.RRF", rrf(atom()) + "missing|fields|\n")
        with self.assertRaisesRegex(ValueError, r"(?:row|line).*2|:2"):
            list(MRConsoSource(path).iter_atoms())

    def test_missing_trailing_pipe_is_not_silently_accepted(self):
        path = self.folder / "bad-terminator.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("MRCONSO.RRF", rrf(atom()).rstrip("\n")[:-1] + "\n")
        with self.assertRaises(ValueError):
            list(MRConsoSource(path).iter_atoms())


if __name__ == "__main__":
    unittest.main()
