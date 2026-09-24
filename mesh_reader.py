"""Read an official MeSH descriptor XML snapshot using the standard library.

ZIP and gzip streams are parsed in place. The XML's external DTD is never
downloaded. Its filename supplies a declared schema year, which is checked
against ``expected_year``; this is not independent verification of the release.
Descriptor, concept, and term scopes remain distinct. In particular, terms of
nonpreferred concepts are not flattened into descriptor synonyms.
"""
from __future__ import annotations

import gzip
import hashlib
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import deque
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

_TREE = re.compile(r"[A-Z][0-9]{2}(?:\.[0-9]{3})*\Z")
_DESCRIPTOR_ID = re.compile(r"D[0-9]{6,}\Z")
_CONCEPT_ID = re.compile(r"M[0-9]+\Z")
_DTD_YEAR = re.compile(rb"nlmdescriptorrecordset_([0-9]{4})[0-9]{4}\.dtd", re.I)


def _stat_signature(path: Path) -> tuple:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


@contextmanager
def _open_xml(path: Path, member: str | None):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            candidates = [entry for entry in entries if not entry.is_dir()
                          and entry.filename.lower().endswith(".xml")
                          and "__MACOSX" not in PurePosixPath(entry.filename).parts
                          and not PurePosixPath(entry.filename).name.startswith("._")]
            if member is None:
                if len(candidates) != 1:
                    names = [entry.filename for entry in candidates]
                    raise ValueError(f"ZIP needs one unambiguous XML member; found {names}. "
                                     "Select its exact member name explicitly.")
                chosen = candidates[0]
            else:
                matches = [entry for entry in candidates if entry.filename == member]
                if len(matches) != 1:
                    raise ValueError(f"Selected XML member is absent or duplicated: {member!r}")
                chosen = matches[0]
            if sum(entry.filename == chosen.filename for entry in entries) != 1:
                raise ValueError(f"Duplicate ZIP member name: {chosen.filename!r}")
            if chosen.flag_bits & 1:
                raise ValueError("Encrypted ZIP members are not supported")
            if not chosen.file_size:
                raise ValueError("Selected MeSH XML member is empty")
            with archive.open(chosen) as stream:
                yield stream, chosen.filename
    else:
        if member is not None:
            raise ValueError("Member selection is valid only for ZIP input")
        with path.open("rb") as source:
            magic = source.read(2)
        opener = gzip.open if magic == b"\x1f\x8b" else open
        with opener(path, "rb") as stream:
            yield stream, None


def _required_text(element: ET.Element, xpath: str, context: str) -> str:
    values = element.findall(xpath)
    if len(values) != 1 or values[0].text is None or not values[0].text.strip():
        raise ValueError(f"{context}: expected one nonempty {xpath}")
    return values[0].text


def _parse_descriptor(record: ET.Element) -> dict:
    descriptor_id = _required_text(record, "DescriptorUI", "DescriptorRecord")
    if not _DESCRIPTOR_ID.fullmatch(descriptor_id):
        raise ValueError(f"Invalid descriptor ID: {descriptor_id!r}")
    label = _required_text(record, "DescriptorName/String", descriptor_id)
    tree_numbers = []
    for element in record.findall("TreeNumberList/TreeNumber"):
        tree = element.text or ""
        if not _TREE.fullmatch(tree):
            raise ValueError(f"{descriptor_id}: invalid MeSH tree number {tree!r}")
        if tree in tree_numbers:
            raise ValueError(f"{descriptor_id}: duplicate tree number {tree!r}")
        tree_numbers.append(tree)
    concepts, scope_notes, relations = [], [], []
    concept_ids, seen_relations = set(), set()
    for concept in record.findall("ConceptList/Concept"):
        concept_id = _required_text(concept, "ConceptUI", descriptor_id)
        if not _CONCEPT_ID.fullmatch(concept_id) or concept_id in concept_ids:
            raise ValueError(f"{descriptor_id}: invalid or duplicate concept ID {concept_id!r}")
        concept_ids.add(concept_id)
        preferred_flag = concept.get("PreferredConceptYN")
        if preferred_flag not in {"Y", "N"}:
            raise ValueError(f"{descriptor_id}/{concept_id}: invalid PreferredConceptYN")
        preferred = preferred_flag == "Y"
        concept_label = _required_text(concept, "ConceptName/String", concept_id)
        terms = []
        for term in concept.findall("TermList/Term"):
            text = _required_text(term, "String", concept_id)
            if text not in terms:
                terms.append(text)
        concepts.append({"id": concept_id, "preferred": preferred,
                         "label": concept_label, "terms": terms})
        for note in concept.findall("ScopeNote"):
            if note.text and note.text.strip():
                scope_notes.append({"concept_id": concept_id, "preferred": preferred,
                                    "text": note.text.strip()})
        for relation in concept.findall("ConceptRelationList/ConceptRelation"):
            name = relation.get("RelationName", "")
            one = _required_text(relation, "Concept1UI", concept_id)
            two = _required_text(relation, "Concept2UI", concept_id)
            if not name or not _CONCEPT_ID.fullmatch(one) or not _CONCEPT_ID.fullmatch(two):
                raise ValueError(f"{descriptor_id}: invalid concept relation")
            identity = name, one, two
            if identity not in seen_relations:
                seen_relations.add(identity)
                relations.append({"relation_name": name, "concept1_id": one, "concept2_id": two})
    if not concepts or sum(concept["preferred"] for concept in concepts) != 1:
        raise ValueError(f"{descriptor_id}: expected exactly one preferred concept")
    for relation in relations:
        if relation["concept1_id"] not in concept_ids or relation["concept2_id"] not in concept_ids:
            raise ValueError(f"{descriptor_id}: concept relation refers outside its descriptor")
    return {"id": descriptor_id, "label": label, "tree_numbers": tree_numbers,
            "scope_notes": scope_notes, "concepts": concepts, "concept_relations": relations}


def _hierarchy(descriptors: dict) -> tuple:
    trees = {}
    for descriptor_id, record in descriptors.items():
        for tree in record["tree_numbers"]:
            if tree in trees:
                raise ValueError(f"Tree {tree} is assigned to both {trees[tree]} and {descriptor_id}")
            trees[tree] = descriptor_id
    parents = {descriptor_id: set() for descriptor_id in descriptors}
    tree_parents, root_trees = [], set()
    for tree, descriptor_id in sorted(trees.items()):
        if "." not in tree:
            root_trees.add(tree)
            continue
        parent_tree = tree.rsplit(".", 1)[0]
        parent_id = trees.get(parent_tree)
        if parent_id is None:
            raise ValueError(f"Tree {tree}: missing parent tree position {parent_tree}")
        parents[descriptor_id].add(parent_id)
        tree_parents.append({"child_tree": tree, "parent_tree": parent_tree,
                             "child_id": descriptor_id, "parent_id": parent_id})
    # Descriptor-level projection can have cycles even when each tree path is
    # acyclic; validate that projection explicitly before ontology integration.
    children = {descriptor_id: set() for descriptor_id in descriptors}
    degrees = {descriptor_id: len(values) for descriptor_id, values in parents.items()}
    for child, values in parents.items():
        for parent in values:
            children[parent].add(child)
    queue = deque(key for key, degree in degrees.items() if not degree)
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for child in children[node]:
            degrees[child] -= 1
            if not degrees[child]:
                queue.append(child)
    if visited != len(descriptors):
        remaining = sorted(key for key, degree in degrees.items() if degree)[:10]
        raise ValueError(f"MeSH descriptor hierarchy contains a cycle; affected IDs include {remaining}")
    return parents, tree_parents, sorted(root_trees)


def read_mesh(path, member=None, expected_year="2025") -> dict:
    """Read and validate a local official MeSH DescriptorRecordSet XML snapshot.

    ``path`` accepts XML, XML.gz, or ZIP with one XML member. Supply ``member``
    for an ambiguous ZIP. SHA256 covers the supplied (possibly compressed) file.
    ``expected_year`` checks the DTD filename's declared year. With None, a DTD
    year is retained if available but is not required. No DTD is fetched.

    Returned ``parents`` maps every descriptor ID to a set of immediate broader
    descriptor IDs. ``tree_parents`` preserves individual position edges; every
    dotted position must have its immediate parent present. ``root_trees`` lists
    actual top-level positions such as C04. Descriptors without tree positions
    remain in ``descriptors``.
    """
    path = Path(path).expanduser().resolve()
    if not path.is_file() or not path.stat().st_size:
        raise ValueError(f"Missing or empty MeSH input: {path}")
    if expected_year is not None and not re.fullmatch(r"[0-9]{4}", str(expected_year)):
        raise ValueError("expected_year must be a four-digit year or None")
    initial_stat = _stat_signature(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    descriptors = {}
    try:
        with _open_xml(path, member) as (stream, chosen_member):
            header = stream.read(65536)
            root_match = re.search(rb"<DescriptorRecordSet(?:\s|>)", header)
            if root_match is None:
                raise ValueError("Expected DescriptorRecordSet root within the first 64 KiB")
            prolog = header[:root_match.start()]
            if b"<!ENTITY" in prolog or re.search(rb"<!DOCTYPE[^>]*\[", prolog, re.S):
                raise ValueError("XML internal subsets and entity declarations are not supported")
            years = set(match.decode("ascii") for match in _DTD_YEAR.findall(prolog))
            if len(years) > 1:
                raise ValueError("Conflicting MeSH DTD years in XML header")
            declared_year = next(iter(years), None)
            if expected_year is not None and declared_year != str(expected_year):
                raise ValueError(f"MeSH DTD declared year {declared_year!r} does not match "
                                 f"expected year {str(expected_year)!r}")
            stream.seek(0)
            depth = 0
            root = None
            for event, element in ET.iterparse(stream, events=("start", "end")):
                if event == "start":
                    depth += 1
                    if root is None:
                        root = element
                        if element.tag != "DescriptorRecordSet":
                            raise ValueError("Expected DescriptorRecordSet document root")
                    elif depth == 2 and element.tag != "DescriptorRecord":
                        raise ValueError(f"Unexpected element under DescriptorRecordSet: {element.tag}")
                else:
                    if depth == 2:
                        record = _parse_descriptor(element)
                        descriptor_id = record["id"]
                        if descriptor_id in descriptors:
                            raise ValueError(f"Duplicate descriptor ID: {descriptor_id}")
                        descriptors[descriptor_id] = record
                        # The compact dicts are kept, not the large XML records.
                        root.remove(element)
                        element.clear()
                    depth -= 1
    except (ET.ParseError, zipfile.BadZipFile, EOFError, zlib.error, UnicodeError) as exc:
        raise ValueError(f"Invalid MeSH XML/archive: {exc}") from exc
    if not descriptors:
        raise ValueError("MeSH descriptor file contains no DescriptorRecord entries")
    if _stat_signature(path) != initial_stat:
        raise ValueError("MeSH input changed while it was being read")
    parents, tree_parents, root_trees = _hierarchy(descriptors)
    counts = {
        "descriptors": len(descriptors),
        "tree_numbers": sum(len(record["tree_numbers"]) for record in descriptors.values()),
        "tree_edges": len(tree_parents),
        "descriptor_parent_edges": sum(len(values) for values in parents.values()),
        "root_trees": len(root_trees),
        "descriptors_without_trees": sum(not record["tree_numbers"] for record in descriptors.values()),
        "concepts": sum(len(record["concepts"]) for record in descriptors.values()),
        "terms": sum(len(concept["terms"]) for record in descriptors.values() for concept in record["concepts"]),
        "scope_notes": sum(len(record["scope_notes"]) for record in descriptors.values()),
        "concept_relations": sum(len(record["concept_relations"]) for record in descriptors.values()),
    }
    return {"descriptors": descriptors, "parents": parents, "tree_parents": tree_parents,
            "root_trees": root_trees,
            "provenance": {"file_sha256": digest.hexdigest(), "member": chosen_member,
                           "declared_year": declared_year, "file_name": path.name},
            "counts": counts}
