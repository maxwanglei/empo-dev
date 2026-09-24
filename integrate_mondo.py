#!/usr/bin/env python3
"""Add MONDO's human disease taxonomy and its own ICD-10-CM mappings to EMPO.

Python 3.10+, standard library only. Inputs stay unchanged. No CUI, label,
MeSH, prefix, or descendant-based mapping is inferred.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET

from mondo_reader import read_mondo, HUMAN_ROOT

RDF = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'
RDFS = 'http://www.w3.org/2000/01/rdf-schema#'
OWL = 'http://www.w3.org/2002/07/owl#'
OIO = 'http://www.geneontology.org/formats/oboInOwl#'
SKOS = 'http://www.w3.org/2004/02/skos/core#'
MONDO = 'http://purl.obolibrary.org/obo/MONDO_'
ABOUT, RESOURCE = '{'+RDF+'}about', '{'+RDF+'}resource'
CLASS, SUBCLASS = '{'+OWL+'}Class', '{'+RDFS+'}subClassOf'
ONTOLOGY = '{'+OWL+'}Ontology'
SKOS_MAPPINGS = {SKOS+n for n in ('exactMatch', 'closeMatch', 'broadMatch', 'narrowMatch', 'relatedMatch')}
ICD_KINDS = {'code', 'block', 'chapter', 'analytical_group', 'unresolved_group'}
OWL_NAME = 'empo_icd_umls_mesh_mondo.owl'
REPORT_NAME = 'mondo_integration_report.json'
SCOPE_NOTE = ('Human disease named-class taxonomy and annotations from the pinned MONDO release. '
              'All active MONDO descendants of MONDO:0700096 and every named parent edge within '
              'that branch are retained. Out-of-branch parents and complex logical axioms are '
              'audited separately. This is a taxonomy subset, not a complete logical module. '
              'Local ICD mapping annotations use only MONDO-supplied ICD10CM identifiers and '
              'do not assert class equivalence or subsumption.')


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def signature(element):
    return (element.tag, tuple(sorted(element.attrib.items())),
            element.text if element.text and element.text.strip() else '',
            tuple(signature(c) for c in element))


def digest(element):
    return hashlib.sha256(repr(signature(element)).encode()).hexdigest()


def literal(node, base, name, value):
    ET.SubElement(node, '{'+base+'}'+name).text = str(value)


def link(node, base, name, iri):
    ET.SubElement(node, '{'+base+'}'+name, {RESOURCE: iri})


def normalize_identifier(text):
    """Normalize display formatting, preserving ranges and all code characters."""
    return re.sub(r'\s+', '', text.upper().replace('.', '').replace('–', '-').replace('—', '-'))


def valid_identifier(value):
    code = r'[A-Z][0-9][A-Z0-9]{1,5}'
    return bool(re.fullmatch(code + '(?:-' + code + ')?', value))


def endpoint_identifier(node, kind, base):
    """Use official identifiers or the documented Step 1 range representation."""
    explicit = node.findall('{'+base+'}ICD_Code')
    if len(explicit) > 1:
        raise ValueError('Multiple ICD_Code values on an existing class.')
    value = normalize_identifier(explicit[0].text or '') if explicit else ''
    origin = 'ICD_Code' if explicit else 'unavailable'
    if kind == 'code':
        if not value or not valid_identifier(value) or '-' in value:
            raise ValueError('Official code node has no valid ICD_Code identifier.')
        return value, origin
    if kind not in {'block', 'chapter'}:
        return value, origin
    if not value and kind == 'block':
        iri = node.get(ABOUT, '')
        prefix = base+'ICD_Block_'
        if iri.startswith(prefix):
            value = iri[len(prefix):].replace('_', '-')
            origin = 'step1_block_iri'
    if not value and kind == 'chapter':
        # Prefer the original official name: the displayed label may be UMLS-enriched.
        names = [n.text or '' for n in node.findall('{'+base+'}icdPreferredName')]
        if not names:
            names = [n.text or '' for n in node.findall('{'+RDFS+'}label')]
        found = {normalize_identifier(m.group(1)) for name in names
                 if (m := re.search(r'\(([A-Z][0-9][A-Z0-9](?:\.[A-Z0-9]+)?\s*-\s*[A-Z][0-9][A-Z0-9](?:\.[A-Z0-9]+)?)\)\s*$', name))}
        if len(found) > 1:
            raise ValueError('Conflicting official ranges on an ICD chapter.')
        if found:
            value, origin = found.pop(), 'official_chapter_name_range'
    if value and valid_identifier(value) and '-' not in value and kind == 'block':
        value, origin = value+'-'+value, origin+'_singleton_range'
    if not valid_identifier(value) or '-' not in value:
        return '', 'unavailable'
    return value, origin


def load_input(path):
    tree = ET.parse(path)
    root = tree.getroot()
    if root.tag != '{'+RDF+'}RDF':
        raise ValueError('Expected EMPO RDF/XML input.')
    classes = root.findall(CLASS)
    by_iri = {n.get(ABOUT): n for n in classes}
    if None in by_iri or len(classes) != len(by_iri):
        raise ValueError('Input has unnamed or duplicate class declarations.')
    if any(i.startswith(MONDO) for i in by_iri):
        raise ValueError('Input already contains MONDO classes; use the pre-MONDO OWL.')
    bases = {c.tag[1:].split('}', 1)[0] for n in classes for c in n if c.tag.endswith('}ICD_Code')}
    if len(bases) != 1:
        raise ValueError('Cannot identify a unique ICD annotation namespace.')
    base = bases.pop()
    ontologies = root.findall(ONTOLOGY)
    if len(ontologies) != 1:
        raise ValueError('Expected one EMPO ontology declaration.')
    ontology = ontologies[0]
    if ontology.find('{'+base+'}mondoAnnotationRelease') is not None or root.find('.//{'+base+'}hasICDMapping') is not None:
        raise ValueError('Input already contains a MONDO integration.')
    nodes, index = [], defaultdict(list)
    for iri, node in by_iri.items():
        kinds = node.findall('{'+base+'}nodeKind')
        if len(kinds) > 1:
            raise ValueError('Ambiguous nodeKind on an original class.')
        kind = kinds[0].text if kinds else ''
        if kind not in ICD_KINDS:
            continue
        code, origin = endpoint_identifier(node, kind, base)
        record = {'class_iri': iri, 'icd_code': code, 'node_kind': kind, 'identifier_origin': origin}
        nodes.append(record)
        if kind in {'code', 'block', 'chapter'} and code:
            index[code].append(record)
    if not any(n['node_kind'] == 'code' for n in nodes):
        raise ValueError('No official ICD code nodes in input.')
    return tree, base, by_iri, ontology, nodes, index


def write_csv(path, fields, rows):
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def declare(root, base, names):
    existing = {n.get(ABOUT) for n in root.findall('{'+OWL+'}AnnotationProperty')}
    for name, description in names.items():
        if base+name not in existing:
            node = ET.SubElement(root, '{'+OWL+'}AnnotationProperty', {ABOUT: base+name})
            literal(node, RDFS, 'label', name)
            literal(node, RDFS, 'comment', description)


def validate_serialized(path, expected, original_classes, original_class_digests, records, links_expected, base):
    """Independent serialized-content check with exact hierarchy and link sets."""
    depth, seen, classes, parents, links_seen = 0, [], {}, set(), set()
    for event, node in ET.iterparse(path, events=('start', 'end')):
        if event == 'start':
            depth += 1
            if depth == 1:
                root = node
            continue
        if depth == 2:
            seen.append(digest(node))
            if node.tag == CLASS:
                iri = node.get(ABOUT)
                if iri in classes:
                    raise ValueError('Duplicate class in serialized output.')
                classes[iri] = digest(node)
                if iri in records:
                    if node.findall('{'+OWL+'}equivalentClass'):
                        raise ValueError('Unexpected logical equivalentClass in MONDO subset.')
                    parents.update((iri, n.get(RESOURCE)) for n in node.findall(SUBCLASS))
                    links_seen.update((iri, n.get(RESOURCE)) for n in node.findall('{'+base+'}hasICDMapping'))
            root.remove(node)
            node.clear()
        depth -= 1
    if seen != expected:
        raise ValueError('Serialization changed planned ontology content.')
    if set(classes) != set(original_classes) | set(records):
        raise ValueError('Unexpected or missing classes after integration.')
    if any(classes[iri] != value for iri, value in original_class_digests.items()):
        raise ValueError('An original ICD/MeSH/population class changed.')
    if parents != {(iri, p) for iri, r in records.items() for p in r['parents']}:
        raise ValueError('MONDO hierarchy differs from its source subset.')
    if links_seen != links_expected:
        raise ValueError('MONDO ICD links differ from mapping audit.')


def integrate(owl_path, mondo_path, output_dir, *, member=None, progress=None):
    owl_path, mondo_path, output_dir = map(Path, (owl_path, mondo_path, output_dir))
    if output_dir.exists():
        raise ValueError('Output directory already exists; choose a new directory.')
    log = progress or (lambda text: None)
    inputs = {str(p.resolve()): sha256(p) for p in (owl_path, mondo_path)}
    log('Reading the existing EMPO ontology')
    tree, base, original_classes, ontology, icd_nodes, index = load_input(owl_path)
    root = tree.getroot()
    original_class_digests = {iri: digest(n) for iri, n in original_classes.items()}
    original_count = len(root)
    original_digests = [digest(n) for n in root]
    log('Selecting the active MONDO human disease taxonomy and reading source mappings')
    mondo = read_mondo(mondo_path, member=member)
    records, provenance = mondo['records'], mondo['provenance']
    release = provenance['release'] or provenance['version_iri'] or 'unversioned-source'
    declared = {
        'hasICDMapping': 'MONDO-supplied ICD10CM correspondence to an existing EMPO ICD node; source relation and evidence are attached to the axiom. Not an equivalence or subclass assertion.',
        'mondoID': 'Canonical MONDO identifier.',
        'mondoRelease': 'Pinned MONDO release for this imported term.',
        'mondoAnnotationRelease': 'MONDO release used for the human disease taxonomy import.',
        'mondoSourceSHA256': 'SHA256 of the supplied MONDO archive/file.',
        'mondoInputOWL_SHA256': 'SHA256 of the unchanged pre-MONDO EMPO ontology.',
        'mondoSourceVersionIRI': 'Original MONDO version IRI.',
        'mondoImportScope': 'Scope and interpretation of the MONDO subset.',
        'mondoMappingStatus': 'source_asserted: correspondence supplied by MONDO; not a new EMPO clinical review.',
        'mondoMappingSourcePredicate': 'Exact predicate of the source mapping assertion.',
        'mondoMappingSourceTarget': 'Exact source mapping target string; see value kind and datatype fields.',
        'mondoMappingSourceValueKind': 'Whether the source mapping target was an IRI or literal.',
        'mondoMappingSourceDatatype': 'Datatype of a literal source mapping target, if supplied.',
        'mondoMappingSourceLanguage': 'Language of a source mapping literal, if supplied.',
        'mondoMappingSourceQualifiers': 'JSON retaining every source qualifier predicate, value, kind, datatype and language.',
        'mondoMappingAuditRow': 'Evidence row identifier in mondo_icd_mappings.csv.',
    }
    declare(root, base, declared)
    literal(ontology, base, 'mondoAnnotationRelease', release)
    literal(ontology, base, 'mondoSourceSHA256', inputs[str(mondo_path.resolve())])
    literal(ontology, base, 'mondoInputOWL_SHA256', inputs[str(owl_path.resolve())])
    literal(ontology, base, 'mondoImportScope', SCOPE_NOTE)
    if provenance['version_iri']:
        link(ontology, base, 'mondoSourceVersionIRI', provenance['version_iri'])
    # Attribution and licensing for the MONDO-derived module, not an import of its full logic.
    link(ontology, 'http://purl.org/dc/terms/', 'source', provenance['ontology_iri'])
    source_ontology = ET.fromstring(provenance['ontology_xml'])
    for license_node in source_ontology.findall('{http://purl.org/dc/terms/}license'):
        copy = ET.fromstring(ET.tostring(license_node, encoding='unicode'))
        module_license = ET.SubElement(ontology, '{'+RDFS+'}comment')
        module_license.text = 'MONDO-derived content license: '+(copy.get(RESOURCE) or copy.text or '')
    log(f'Importing {len(records):,} human disease classes with {sum(len(r["parents"]) for r in records.values()):,} native parent links')
    mapping_rows, terms, hierarchy = [], [], []
    links_expected, evidence_axioms = set(), []
    coverage = defaultdict(set)
    source_properties, imported_source_axioms = set(), []
    excluded_annotation_rows = []
    for iri, record in records.items():
        node = ET.SubElement(root, CLASS, {ABOUT: iri})
        literal(node, base, 'nodeKind', 'mondo_human_disease')
        literal(node, base, 'mondoID', record['id'])
        literal(node, base, 'mondoRelease', release)
        icd_assertions = {(m['source_predicate'], m['target']) for m in record['icd_mappings']}
        excluded_assertions = set()
        for xml in record['annotations']:
            child = ET.fromstring(xml)
            predicate = child.tag[1:].replace('}', '', 1)
            target = child.get(RESOURCE) or child.text or ''
            if predicate in SKOS_MAPPINGS and (predicate, target) not in icd_assertions:
                excluded_assertions.add((predicate, target))
                excluded_annotation_rows.append({'class_iri': iri, 'predicate': predicate,
                    'reason': 'non_icd_mapping_outside_requested_scope', 'target': target, 'source_xml': xml})
                continue
            node.append(child)
            source_properties.add(predicate)
        for parent in sorted(record['parents']):
            link(node, RDFS, 'subClassOf', parent)
            hierarchy.append({'child_iri': iri, 'parent_iri': parent})
        for xml in record['source_axioms']:
            ax = ET.fromstring(xml)
            predicate_node = ax.find('{'+OWL+'}annotatedProperty')
            target_node = ax.find('{'+OWL+'}annotatedTarget')
            predicate = predicate_node.get(RESOURCE) if predicate_node is not None else ''
            target = (target_node.get(RESOURCE) or target_node.text or '') if target_node is not None else ''
            if (predicate, target) in excluded_assertions:
                excluded_annotation_rows.append({'class_iri': iri, 'predicate': predicate,
                    'reason': 'annotation_on_non_icd_mapping', 'target': target, 'source_xml': xml})
                continue
            imported_source_axioms.append(ax)
            source_properties.update(c.tag[1:].replace('}', '', 1) for c in ax if c.tag not in
                                     {'{'+OWL+'}annotatedSource', '{'+OWL+'}annotatedProperty', '{'+OWL+'}annotatedTarget'})
        linked_here = set()
        for mapping in record['icd_mappings']:
            namespace, code = mapping['namespace'], normalize_identifier(mapping['code'])
            targets = []
            if namespace.upper() != 'ICD10CM':
                status = 'unsupported_icd_namespace'
            elif not valid_identifier(code):
                status = 'invalid_icd_identifier'
            elif len(index.get(code, [])) > 1:
                status = 'ambiguous_icd_endpoint'
            elif not index.get(code):
                status = 'absent_icd_endpoint'
            else:
                status, targets = 'linked', index[code]
            row_id = len(mapping_rows)+1
            mapping_rows.append({'evidence_id': row_id, 'mondo_iri': iri, 'mondo_id': record['id'],
                'mondo_label': record['label'], 'source_predicate': mapping['source_predicate'],
                'source_target': mapping['target'], 'value_kind': mapping['value_kind'],
                'source_datatype': mapping['datatype'] or '', 'source_language': mapping['lang'] or '',
                'icd_namespace': namespace, 'icd_code': code, 'status': status,
                'matched_icd_iris': json.dumps([t['class_iri'] for t in targets]),
                'matched_node_kinds': json.dumps([t['node_kind'] for t in targets]),
                'source_qualifiers': json.dumps(mapping['qualifiers'], ensure_ascii=False),
                'source_status': 'source_asserted'})
            for target in targets:
                target_iri = target['class_iri']
                linked_here.add(target_iri)
                links_expected.add((iri, target_iri))
                coverage[target_iri].add(iri)
                ax = ET.Element('{'+OWL+'}Axiom')
                link(ax, OWL, 'annotatedSource', iri)
                link(ax, OWL, 'annotatedProperty', base+'hasICDMapping')
                link(ax, OWL, 'annotatedTarget', target_iri)
                literal(ax, base, 'mondoMappingStatus', 'source_asserted')
                link(ax, base, 'mondoMappingSourcePredicate', mapping['source_predicate'])
                literal(ax, base, 'mondoMappingSourceTarget', mapping['target'])
                literal(ax, base, 'mondoMappingSourceValueKind', mapping['value_kind'])
                if mapping['datatype']:
                    literal(ax, base, 'mondoMappingSourceDatatype', mapping['datatype'])
                if mapping['lang']:
                    literal(ax, base, 'mondoMappingSourceLanguage', mapping['lang'])
                literal(ax, base, 'mondoMappingSourceQualifiers', json.dumps(mapping['qualifiers'], ensure_ascii=False))
                literal(ax, base, 'mondoMappingAuditRow', row_id)
                literal(ax, base, 'mondoSourceSHA256', inputs[str(mondo_path.resolve())])
                evidence_axioms.append(ax)
        for target_iri in sorted(linked_here):
            link(node, base, 'hasICDMapping', target_iri)
        terms.append({'mondo_iri': iri, 'mondo_id': record['id'], 'label': record['label'],
                      'parent_count': len(record['parents']), 'icd_node_count': len(linked_here), 'release': release})
    # Declare source annotation predicates, retaining their labels/comments without
    # importing extra ontology classes or object-property logic through metadata.
    declared_iris = {n.get(ABOUT) for n in root if n.get(ABOUT)}
    for predicate in sorted(source_properties):
        if predicate in declared_iris or predicate in {RDFS+'label', RDFS+'comment'}:
            continue
        declaration = ET.SubElement(root, '{'+OWL+'}AnnotationProperty', {ABOUT: predicate})
        if predicate in mondo['annotation_properties']:
            original = ET.fromstring(mondo['annotation_properties'][predicate])
            for child in original:
                if child.tag in {'{'+RDFS+'}label', '{'+RDFS+'}comment'}:
                    declaration.append(child)
    root.extend(imported_source_axioms)
    root.extend(evidence_axioms)
    # Every original top-level element stays byte-equivalent in RDF/XML structure,
    # except explicitly appended metadata on the ontology declaration.
    for position, old_digest in enumerate(original_digests):
        if root[position].tag != ONTOLOGY and digest(root[position]) != old_digest:
            raise ValueError('Existing ontology content changed during integration.')
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.mondo-stage-', dir=output_dir.parent))
    published = False
    try:
        log('Writing the ontology, source mapping audit, and all-level ICD coverage')
        for prefix, uri in [('rdf', RDF), ('rdfs', RDFS), ('owl', OWL), ('empo', base), ('obo', 'http://purl.obolibrary.org/obo/'), ('oboInOwl', OIO), ('skos', SKOS)]:
            ET.register_namespace(prefix, uri)
        expected = [digest(n) for n in root]
        ET.indent(root, space='  ')
        tree.write(stage/OWL_NAME, encoding='utf-8', xml_declaration=True)
        write_csv(stage/'mondo_human_terms.csv', ['mondo_iri','mondo_id','label','parent_count','icd_node_count','release'], terms)
        write_csv(stage/'mondo_hierarchy.csv', ['child_iri','parent_iri'], hierarchy)
        mapping_fields = ['evidence_id','mondo_iri','mondo_id','mondo_label','source_predicate','source_target','value_kind','source_datatype','source_language','icd_namespace','icd_code','status','matched_icd_iris','matched_node_kinds','source_qualifiers','source_status']
        write_csv(stage/'mondo_icd_mappings.csv', mapping_fields, mapping_rows)
        coverage_rows = []
        for item in sorted(icd_nodes, key=lambda row: row['class_iri']):
            kind = item['node_kind']
            count = len(coverage[item['class_iri']])
            status = ('mapped' if count else 'not_eligible_analytical_or_unresolved' if kind in {'analytical_group','unresolved_group'}
                      else 'identifier_unavailable' if not item['icd_code'] else 'no_mondo_mapping')
            coverage_rows.append(dict(item, mondo_mapping_count=count, status=status))
        write_csv(stage/'icd_mondo_coverage.csv', ['class_iri','icd_code','node_kind','identifier_origin','mondo_mapping_count','status'], coverage_rows)
        write_csv(stage/'mondo_boundary_parents.csv', ['child_iri','parent_iri','reason'], mondo['boundary_parents'])
        omitted_rows = mondo['omitted_axioms']+excluded_annotation_rows
        write_csv(stage/'mondo_omitted_axioms.csv', ['class_iri','predicate','reason','target','source_xml'], omitted_rows)
        log('Verifying every original class, every MONDO parent edge, and every mapping link')
        validate_serialized(stage/OWL_NAME, expected, original_classes, original_class_digests, records, links_expected, base)
        expected_csv_counts = {'mondo_human_terms.csv':len(terms), 'mondo_hierarchy.csv':len(hierarchy),
            'mondo_icd_mappings.csv':len(mapping_rows), 'icd_mondo_coverage.csv':len(coverage_rows),
            'mondo_boundary_parents.csv':len(mondo['boundary_parents']), 'mondo_omitted_axioms.csv':len(omitted_rows)}
        for name, count in expected_csv_counts.items():
            with (stage/name).open(encoding='utf-8', newline='') as stream:
                if sum(1 for _ in csv.DictReader(stream)) != count:
                    raise ValueError('Incomplete export: '+name)
        for path in (owl_path, mondo_path):
            if sha256(path) != inputs[str(path.resolve())]:
                raise ValueError('Input changed during integration: '+str(path))
        pairs = {(r['mondo_iri'],r['icd_namespace'],r['icd_code'],r['status']) for r in mapping_rows}
        cm_pairs = {p for p in pairs if p[1].upper() == 'ICD10CM'}
        kinds_by_iri = {r['class_iri']:r['node_kind'] for r in icd_nodes}
        report = {'mondo_release':release, 'human_root_iri':HUMAN_ROOT, 'scope':SCOPE_NOTE,
            'input_sha256':inputs, 'mondo_source':{k:v for k,v in provenance.items() if k != 'ontology_xml'},
            'original_classes':len(original_classes), 'mondo_human_classes':len(records),
            'total_output_classes':len(original_classes)+len(records), 'mondo_subclass_edges':len(hierarchy),
            'multiple_parent_mondo_classes':sum(len(r['parents'])>1 for r in records.values()),
            'boundary_parent_edges':len(mondo['boundary_parents']), 'source_parser_counts':mondo['counts'],
            'retained_source_axioms':len(imported_source_axioms), 'omitted_axiom_reasons':dict(Counter(r['reason'] for r in omitted_rows)),
            'mapping_evidence_rows':len(mapping_rows), 'mapping_evidence_status_counts':dict(Counter(r['status'] for r in mapping_rows)),
            'source_icd10cm_pairs':len(cm_pairs), 'icd10cm_pair_status_counts':dict(Counter(p[3] for p in cm_pairs)),
            'mondo_icd_links':len(links_expected), 'mapped_icd_nodes':len({i for _,i in links_expected}),
            'mondo_terms_with_icd_links':len({m for m,_ in links_expected}),
            'links_by_icd_node_kind':dict(Counter(kinds_by_iri[i] for _,i in links_expected)),
            'coverage_nodes':len(coverage_rows), 'coverage_by_node_kind':dict(Counter(r['node_kind'] for r in coverage_rows)),
            'validation':{'xml_parses':True,'original_class_content_preserved':True,'native_human_hierarchy_exact':True,
                'source_mapping_links_exact':True,'all_csv_row_counts_verified':True,'input_hashes_unchanged':True,
                'owl_reasoner_run':False,'new_cross_vocabulary_logical_axioms':0},
            'output_sha256':{name:sha256(stage/name) for name in sorted([OWL_NAME, *expected_csv_counts])}}
        (stage/REPORT_NAME).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        if output_dir.exists():
            raise ValueError('Output directory appeared during integration; refusing to overwrite it.')
        stage.rename(output_dir)
        published=True
        log(f'Finished: {len(records):,} MONDO classes, {len(hierarchy):,} parent edges, {len(links_expected):,} ICD mapping links')
        return report
    finally:
        if not published:
            shutil.rmtree(stage, ignore_errors=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owl',type=Path,required=True,help='Existing ICD/UMLS/MeSH EMPO OWL')
    parser.add_argument('--mondo',type=Path,required=True,help='Official MONDO RDF/XML, ZIP or gzip')
    parser.add_argument('--output-dir',type=Path,default=Path('output_mondo'),help='Fresh output directory')
    parser.add_argument('--mondo-member',help='Exact OWL member if the ZIP is ambiguous')
    args=parser.parse_args()
    try:
        integrate(args.owl,args.mondo,args.output_dir,member=args.mondo_member,
                  progress=lambda text: print(text,file=sys.stderr,flush=True))
    except (OSError,ValueError,ET.ParseError) as exc:
        parser.exit(2,f'MONDO integration failed: {exc}\n')
    print(f'Written: {args.output_dir/OWL_NAME}')


if __name__=='__main__':
    main()
