"""Read a MONDO RDF/XML snapshot as an explicitly scoped human disease taxonomy.

This is a named-class taxonomy/annotation extraction, not an OWL logical module.
It does not run a reasoner or fetch imports. Only active canonical MONDO classes
reachable through named ``rdfs:subClassOf`` edges from the chosen root are kept.
All named edges within that selection and simple source annotations are retained.
Logical axioms outside this scope and boundary parents are explicitly audited.
"""
from __future__ import annotations

import gzip
import hashlib
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter, defaultdict, deque
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"
XML = "http://www.w3.org/XML/1998/namespace"
OIO = "http://www.geneontology.org/formats/oboInOwl#"
SKOS = "http://www.w3.org/2004/02/skos/core#"
MONDO = "http://purl.obolibrary.org/obo/MONDO_"
HUMAN_ROOT = MONDO + "0700096"
AB = "{" + RDF + "}about"
RS = "{" + RDF + "}resource"
DT = "{" + RDF + "}datatype"
LANG = "{" + XML + "}lang"
BASE = "{" + XML + "}base"
CLASS = "{" + OWL + "}Class"
AXIOM = "{" + OWL + "}Axiom"
SUB = "{" + RDFS + "}subClassOf"
LABEL = "{" + RDFS + "}label"
DEPRECATED = "{" + OWL + "}deprecated"
LOGICAL = {SUB, "{" + OWL + "}equivalentClass", "{" + OWL + "}disjointWith",
           "{" + OWL + "}disjointUnionOf", "{" + OWL + "}intersectionOf",
           "{" + OWL + "}unionOf", "{" + OWL + "}complementOf",
           "{" + OWL + "}oneOf", "{" + OWL + "}hasKey"}
MAPPING_PREDICATES = {OIO + "hasDbXref", OWL + "equivalentClass"} | {
    SKOS + local for local in ("exactMatch", "closeMatch", "broadMatch", "narrowMatch", "relatedMatch")}
MONDO_ID = re.compile(r"http://purl\.obolibrary\.org/obo/MONDO_[0-9]{7}\Z")


def _iri(tag: str) -> str:
    if not tag.startswith("{") or "}" not in tag:
        raise ValueError(f"Expected namespace-qualified RDF property: {tag!r}")
    return tag[1:].replace("}", "", 1)


def _stat(path: Path) -> tuple:
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@contextmanager
def _open_xml(path: Path, member: str | None):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            entries = z.infolist()
            choices = [i for i in entries if not i.is_dir()
                       and i.filename.lower().endswith((".owl", ".rdf", ".xml"))
                       and "__MACOSX" not in PurePosixPath(i.filename).parts
                       and not PurePosixPath(i.filename).name.startswith("._")]
            if member is None:
                if len(choices) != 1:
                    raise ValueError("ZIP needs one unambiguous RDF/XML member; select --member explicitly: "
                                     + repr([i.filename for i in choices]))
                chosen = choices[0]
            else:
                matches = [i for i in choices if i.filename == member]
                if len(matches) != 1:
                    raise ValueError(f"Selected RDF/XML member is absent or duplicated: {member!r}")
                chosen = matches[0]
            if sum(i.filename == chosen.filename for i in entries) != 1:
                raise ValueError(f"Duplicate ZIP member name: {chosen.filename!r}")
            if chosen.flag_bits & 1 or not chosen.file_size:
                raise ValueError("MONDO XML member must be nonempty and unencrypted")
            with z.open(chosen) as f:
                yield f, chosen.filename
    else:
        if member is not None:
            raise ValueError("Member selection is valid only for ZIP input")
        with path.open("rb") as f:
            magic = f.read(2)
        with (gzip.open if magic == b"\x1f\x8b" else open)(path, "rb") as f:
            yield f, None


def _absolutize(element: ET.Element, base: str, language: str | None = None) -> None:
    """Make source fragments independent of inherited xml:base/xml:lang."""
    here = urljoin(base, element.get(BASE, ""))
    lang = element.get(LANG, language)
    for attr in (AB, RS, DT):
        if attr in element.attrib:
            value = urljoin(here, element.attrib[attr])
            if not urlsplit(value).scheme:
                raise ValueError(f"Relative RDF IRI without resolvable xml:base: {value!r}")
            element.set(attr, value)
    element.attrib.pop(BASE, None)
    if lang is not None and LANG not in element.attrib:
        element.set(LANG, lang)
    for child in element:
        _absolutize(child, here, lang)


def _check_prolog(header: bytes) -> None:
    """Reject DTDs before ElementTree sees them, including long-comment tricks.

    The supported source serialization uses UTF-8/ASCII and begins its root
    within 64 KiB. Bounded prolog parsing cannot overlook a declaration after
    a long comment, since oversized/incomplete prologs are rejected outright.
    """
    if b"\x00" in header or header.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValueError("MONDO RDF/XML must use UTF-8/ASCII serialization")
    position = 3 if header.startswith(b"\xef\xbb\xbf") else 0
    while position < len(header):
        while position < len(header) and header[position:position + 1] in b" \t\r\n":
            position += 1
        if header.startswith(b"<?", position):
            end = header.find(b"?>", position + 2)
            if end == -1:
                break
            position = end + 2
        elif header.startswith(b"<!--", position):
            end = header.find(b"-->", position + 4)
            if end == -1:
                break
            position = end + 3
        elif header.startswith((b"<!DOCTYPE", b"<!ENTITY"), position):
            raise ValueError("RDF/XML DTD and entity declarations are not supported")
        elif re.match(rb"<[A-Za-z_]", header[position:position + 2]):
            return
        else:
            raise ValueError("Invalid RDF/XML prolog before the document root")
    raise ValueError("RDF/XML document root must begin within the first 64 KiB")


def _elements(path: Path, member: str | None):
    with _open_xml(path, member) as (stream, chosen):
        header = stream.read(65536)
        _check_prolog(header)
        stream.seek(0)
        depth = 0
        root = None
        base = ""
        language = None
        for event, element in ET.iterparse(stream, events=("start", "end")):
            if event == "start":
                depth += 1
                if root is None:
                    root = element
                    if root.tag != "{" + RDF + "}RDF":
                        raise ValueError("Expected RDF/XML rdf:RDF document root")
                    base = root.get(BASE, "")
                    language = root.get(LANG)
            else:
                if depth == 2:
                    _absolutize(element, base, language)
                    yield element, chosen
                    root.remove(element)
                    element.clear()
                depth -= 1


def _value(element: ET.Element, context: str) -> dict:
    """Represent a simple RDF annotation without changing lexical values."""
    if len(element):
        raise ValueError(f"{context}: nested annotation RDF syntax is unsupported")
    allowed = {RS, DT, LANG}
    extra = set(element.attrib) - allowed
    if extra:
        raise ValueError(f"{context}: unsupported annotation attributes {sorted(extra)}")
    if RS in element.attrib:
        if DT in element.attrib or (element.text or "").strip():
            raise ValueError(f"{context}: invalid resource annotation")
        return {"value": element.attrib[RS], "kind": "iri", "datatype": None, "lang": None}
    if DT in element.attrib and element.get(LANG):
        raise ValueError(f"{context}: literal has both datatype and language")
    return {"value": element.text or "", "kind": "literal",
            "datatype": element.get(DT), "lang": element.get(LANG)}


def _serialized(element: ET.Element) -> str:
    tail = element.tail
    element.tail = None
    result = ET.tostring(element, encoding="unicode")
    element.tail = tail
    return result


def _named_parent(element: ET.Element, context: str) -> str | None:
    if RS in element.attrib:
        if len(element) or (element.text or "").strip() or set(element.attrib) - {RS, LANG}:
            raise ValueError(f"{context}: malformed named subclass edge")
        return element.attrib[RS]
    return None


def _class_head(element: ET.Element) -> dict:
    iri = element.get(AB)
    if not iri or set(element.attrib) - {AB, LANG}:
        raise ValueError("Top-level MONDO owl:Class must have an unambiguous rdf:about")
    if not MONDO_ID.fullmatch(iri):
        raise ValueError(f"Invalid canonical MONDO IRI: {iri!r}")
    deprecated = False
    for child in element.findall(DEPRECATED):
        value = _value(child, iri)["value"].strip()
        if value not in {"true", "false", "1", "0"}:
            raise ValueError(f"{iri}: invalid owl:deprecated value {value!r}")
        deprecated |= value in {"true", "1"}
    labels = [(_value(c, iri), c) for c in element.findall(LABEL)]
    if any(value["kind"] != "literal" for value, _ in labels):
        raise ValueError(f"{iri}: rdfs:label must be a literal")
    preferred = [value["value"] for value, _ in labels if value["lang"] in {None, "", "en"}]
    label = preferred[0] if preferred else (labels[0][0]["value"] if labels else "")
    parents = set()
    for child in element.findall(SUB):
        parent = _named_parent(child, iri)
        if parent is not None:
            parents.add(parent)
    return {"iri": iri, "id": "MONDO:" + iri[len(MONDO):], "label": label,
            "deprecated": deprecated, "all_parents": parents}


def _select(records: dict, root_iri: str) -> set:
    if root_iri not in records:
        raise ValueError(f"Human disease root is missing: {root_iri}")
    if records[root_iri]["deprecated"]:
        raise ValueError(f"Human disease root is deprecated: {root_iri}")
    children = defaultdict(set)
    for iri, record in records.items():
        if not record["deprecated"]:
            for parent in record["all_parents"]:
                children[parent].add(iri)
    selected = set()
    queue = deque([root_iri])
    while queue:
        iri = queue.popleft()
        if iri in selected:
            continue
        selected.add(iri)
        queue.extend(children[iri] - selected)
    degrees = {iri: len(records[iri]["all_parents"] & selected) for iri in selected}
    queue = deque(iri for iri, degree in degrees.items() if not degree)
    visited = 0
    while queue:
        iri = queue.popleft()
        visited += 1
        for child in children[iri] & selected:
            degrees[child] -= 1
            if not degrees[child]:
                queue.append(child)
    if visited != len(selected):
        raise ValueError("MONDO human disease named hierarchy contains a cycle: "
                         + repr(sorted(iri for iri, degree in degrees.items() if degree)[:10]))
    return selected


def _icd_target(value: str) -> tuple[str, str] | None:
    # Return source spelling. Crucially, ICD10 is not treated as ICD10CM.
    match = re.fullmatch(r"(ICD[A-Za-z0-9_-]*):(.+)", value)
    if match:
        return match.group(1), match.group(2)
    # Known BioPortal, OBO/PURL and identifiers.org forms. No fuzzy matching.
    match = re.fullmatch(r"https?://purl\.bioontology\.org/ontology/(ICD[A-Za-z0-9_-]*)/(.+)", value)
    if match:
        return match.group(1), match.group(2)
    match = re.fullmatch(r"https?://purl\.obolibrary\.org/obo/(ICD[A-Za-z0-9-]*)_(.+)", value)
    if not match:
        match = re.fullmatch(r"https?://identifiers\.org/(ICD[A-Za-z0-9_-]*)[:/](.+)", value, re.I)
    if match:
        return match.group(1), match.group(2)
    return None


def _key(predicate: str, value: dict) -> tuple:
    return predicate, value["kind"], value["value"], value["datatype"], value["lang"]


def _audit(iri: str, child: ET.Element, reason: str) -> dict:
    return {"class_iri": iri, "predicate": _iri(child.tag), "reason": reason,
            "target": child.get(RS, ""), "source_xml": _serialized(child)}


def read_mondo(path, *, member=None, root_iri=HUMAN_ROOT) -> dict:
    """Read a local .owl/.rdf/.xml, ZIP, or gzip MONDO RDF/XML snapshot.

    ``records`` is keyed by canonical IRI. Each record contains ``id``, ``label``,
    selected ``parents`` and original ``all_parents`` sets, ``annotations`` XML
    fragments, retained ``source_axioms`` XML, and direct ``icd_mappings`` with
    exact source predicate, target, namespace, code, and axiom qualifiers.
    ``boundary_parents`` and ``omitted_axioms`` explain excluded class logic.
    Source annotations (including synonym types) retain their original property,
    literal datatype, language and IRI values. No external ontology is loaded.
    """
    path = Path(path).expanduser().resolve()
    if not path.is_file() or not path.stat().st_size:
        raise ValueError(f"Missing or empty MONDO input: {path}")
    if not MONDO_ID.fullmatch(root_iri):
        raise ValueError("root_iri must be a canonical MONDO class IRI")
    initial = _stat(path)
    sha256 = _sha256(path)
    all_records = {}
    ontologies = []
    annotation_declarations = {}
    chosen_member = None
    top_level_counts = Counter()
    try:
        for element, chosen_member in _elements(path, member):
            top_level_counts[_iri(element.tag)] += 1
            iri = element.get(AB, "")
            if element.tag == CLASS and iri.startswith(MONDO):
                record = _class_head(element)
                if iri in all_records:
                    raise ValueError(f"Duplicate MONDO class declaration: {iri}")
                all_records[iri] = record
            elif element.tag == "{" + RDF + "}Description":
                is_class = any(c.tag == "{" + RDF + "}type" and c.get(RS) == OWL + "Class" for c in element)
                if iri.startswith(MONDO) or is_class:
                    raise ValueError("rdf:Description class syntax is unsupported; use explicit owl:Class declarations")
            elif element.tag == "{" + OWL + "}Ontology":
                versions = [c.get(RS) for c in element if c.tag == "{" + OWL + "}versionIRI"]
                if len(versions) > 1:
                    raise ValueError("Conflicting MONDO ontology version IRIs")
                ontologies.append({"ontology_iri": iri, "version_iri": versions[0] if versions else None,
                                   "source_xml": _serialized(element)})
            elif element.tag == "{" + OWL + "}AnnotationProperty":
                if not iri or iri in annotation_declarations:
                    raise ValueError(f"Missing or duplicate annotation property declaration: {iri!r}")
                annotation_declarations[iri] = _serialized(element)
        if len(ontologies) != 1:
            raise ValueError("Expected exactly one MONDO owl:Ontology declaration")
        selected = _select(all_records, root_iri)
        records = {iri: all_records[iri] for iri in sorted(selected)}
        boundary = []
        omitted = []
        used_properties = set()
        assertion_keys = defaultdict(set)
        mapping_lookup = defaultdict(list)
        all_source_axioms = []
        for iri, record in records.items():
            record.update(parents=record["all_parents"] & selected, annotations=[],
                          source_axioms=[], icd_mappings=[])
            for parent in sorted(record["all_parents"] - selected):
                reason = ("deprecated_parent" if parent in all_records and all_records[parent]["deprecated"]
                          else "outside_human_branch" if parent in all_records else "undeclared_or_external_parent")
                boundary.append({"child_iri": iri, "parent_iri": parent, "reason": reason})
        for element, _ in _elements(path, member):
            iri = element.get(AB, "")
            if element.tag == CLASS and iri in selected:
                record = records[iri]
                for child in element:
                    predicate = _iri(child.tag)
                    if child.tag in LOGICAL:
                        parent = _named_parent(child, iri) if child.tag == SUB else None
                        if child.tag == SUB and parent in selected:
                            assertion_keys[iri].add(_key(predicate, _value(child, iri)))
                        else:
                            reason = "boundary_parent" if child.tag == SUB and parent else "logical_axiom_outside_taxonomy_scope"
                            omitted.append(_audit(iri, child, reason))
                    else:
                        value = _value(child, iri)
                        record["annotations"].append(_serialized(child))
                        used_properties.add(predicate)
                        assertion_keys[iri].add(_key(predicate, value))
                    if predicate in MAPPING_PREDICATES and not len(child) and "{" + RDF + "}nodeID" not in child.attrib:
                        value = _value(child, iri)
                        target = _icd_target(value["value"])
                        if target:
                            mapping = {"source_predicate": predicate, "target": value["value"],
                                       "value_kind": value["kind"], "datatype": value["datatype"],
                                       "lang": value["lang"], "namespace": target[0], "code": target[1],
                                       "qualifiers": [], "source_axioms": []}
                            record["icd_mappings"].append(mapping)
                            mapping_lookup[(iri, _key(predicate, value))].append(mapping)
            elif element.tag == AXIOM:
                sources = element.findall("{" + OWL + "}annotatedSource")
                if len(sources) == 1 and sources[0].get(RS) in selected:
                    all_source_axioms.append(_serialized(element))
        retained_axioms = 0
        for xml in all_source_axioms:
            element = ET.fromstring(xml)
            source = element.find("{" + OWL + "}annotatedSource").get(RS)
            props = element.findall("{" + OWL + "}annotatedProperty")
            targets = element.findall("{" + OWL + "}annotatedTarget")
            if len(props) != 1 or len(targets) != 1 or not props[0].get(RS):
                raise ValueError(f"{source}: malformed owl:Axiom annotation")
            predicate = props[0].get(RS)
            try:
                value = _value(targets[0], source)
            except ValueError:
                # Complex logical assertions are deliberately outside this extraction.
                if predicate not in {_iri(tag) for tag in LOGICAL}:
                    raise
                omitted.append({"class_iri": source, "predicate": predicate,
                                "reason": "annotation_on_omitted_complex_axiom", "target": "", "source_xml": xml})
                continue
            key = _key(predicate, value)
            qualifiers = []
            for child in element:
                if child.tag in {"{" + OWL + "}annotatedSource", "{" + OWL + "}annotatedProperty", "{" + OWL + "}annotatedTarget"}:
                    continue
                qual = {"predicate": _iri(child.tag), **_value(child, source)}
                qualifiers.append(qual)
            for mapping in mapping_lookup.get((source, key), []):
                mapping["qualifiers"].extend(q for q in qualifiers if q not in mapping["qualifiers"])
                mapping["source_axioms"].append(xml)
            if key in assertion_keys[source]:
                records[source]["source_axioms"].append(xml)
                retained_axioms += 1
                used_properties.update(q["predicate"] for q in qualifiers)
            else:
                omitted.append({"class_iri": source, "predicate": predicate,
                                "reason": "annotation_on_omitted_assertion", "target": value["value"], "source_xml": xml})
    except (ET.ParseError, zipfile.BadZipFile, EOFError, zlib.error, UnicodeError) as exc:
        raise ValueError(f"Invalid MONDO RDF/XML/archive: {exc}") from exc
    if _stat(path) != initial or _sha256(path) != sha256:
        raise ValueError("MONDO input changed while it was being read")
    ontology = ontologies[0]
    match = re.search(r"/releases/([0-9]{4}-[0-9]{2}-[0-9]{2})/", ontology["version_iri"] or "")
    # Keep only declarations actually used, then close over annotation properties
    # used to describe those declarations (labels/definitions remain intact).
    declarations = {}
    queue = list(used_properties)
    while queue:
        prop = queue.pop()
        if prop in declarations or prop not in annotation_declarations:
            continue
        xml = annotation_declarations[prop]
        declarations[prop] = xml
        for child in ET.fromstring(xml):
            child_prop = _iri(child.tag)
            if child_prop in annotation_declarations and child_prop not in declarations:
                queue.append(child_prop)
    return {"records": records, "parents": {iri: record["parents"] for iri, record in records.items()},
            "root_iri": root_iri, "boundary_parents": boundary, "omitted_axioms": omitted,
            "annotation_properties": declarations,
            "provenance": {"path": str(path), "sha256": sha256, "member": chosen_member,
                           "ontology_iri": ontology["ontology_iri"], "version_iri": ontology["version_iri"],
                           "release": match.group(1) if match else None, "ontology_xml": ontology["source_xml"]},
            "counts": {"source_mondo_classes": len(all_records), "source_deprecated_mondo_classes": sum(r["deprecated"] for r in all_records.values()),
                       "selected_classes": len(records), "selected_subclass_edges": sum(len(r["parents"]) for r in records.values()),
                       "selected_multiple_parent_classes": sum(len(r["parents"]) > 1 for r in records.values()),
                       "boundary_parent_edges": len(boundary), "omitted_axioms": len(omitted),
                       "retained_source_axioms": retained_axioms,
                       "icd_mapping_assertions": sum(len(r["icd_mappings"]) for r in records.values()),
                       "top_level_elements": dict(top_level_counts)}}
