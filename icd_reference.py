"""Parse pinned CDC ICD-10-CM hierarchies for the 2016–2021 claim interval.

Nodes use stable keys: ``chapter:1``, ``block:A00-A09``, ``code:A000``.
Each node preserves all release observations and chooses the last applicable
label/parent assertion. A dictionary is returned; no old ontology is read.

Billable seventh-character codes are admitted only from an official code/order
file. Their parent must be an explicit tabular node whose inherited seventh
character definition and X padding explain the complete code. Such nodes have
kind='extension' and materialize=False, allowing a caller to validate original
claim codes without placing every encounter-specific code in its output.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date
from pathlib import Path


def normalize_code(value: str) -> str:
    raw = str(value).strip().upper()
    if not re.fullmatch(r"[A-Z][0-9][A-Z0-9](?:\.?[A-Z0-9]{1,4})?", raw):
        raise ValueError(f"Invalid ICD-10-CM code: {value!r}")
    return raw.replace('.', '')


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _verified(base: Path, entry: dict, path_key: str, hash_key: str) -> Path:
    path = (base / entry[path_key]).resolve()
    if not path.is_file():
        raise ValueError(f"Missing reference file: {path}")
    expected = entry.get(hash_key)
    if not expected or _hash(path) != expected:
        raise ValueError(f"Missing or incorrect SHA256 for reference: {path}")
    return path


def _text(node: ET.Element, name: str) -> str:
    child = node.find(name)
    return ''.join(child.itertext()).strip() if child is not None else ''


def _extensions(element, inherited):
    own = {node.get('char', '').upper(): ''.join(node.itertext()).strip()
           for path in ('sevenChrDef/extension', 'seventhCharDef/extension')
           for node in element.findall(path)}
    own.pop('', None)
    return own or inherited


def _parse_xml(path: Path) -> dict:
    root = ET.parse(path).getroot()
    for element in root.iter():
        if isinstance(element.tag, str):
            element.tag = element.tag.rsplit('}', 1)[-1]
    if root.tag != 'ICD10CM.tabular':
        raise ValueError(f"Not an ICD10CM tabular file: {path}")
    nodes = {}

    def add(key, kind, code, label, parent, extensions=None, leaf=False):
        if key in nodes:
            raise ValueError(f"Duplicate tabular node {key} in {path}")
        if not label:
            raise ValueError(f"Empty label for {key} in {path}")
        nodes[key] = {'kind': kind, 'code': code, 'raw_code': code, 'label': label,
                      'parents': [parent] if parent else [], 'materialize': True,
                      '_extensions': extensions or {}, '_leaf': leaf, 'billable': False}

    def walk(element, parent, inherited):
        raw = _text(element, 'name')
        code = normalize_code(raw)
        children = element.findall('diag')
        ext = _extensions(element, inherited)
        key = 'code:' + code
        add(key, 'code', raw, _text(element, 'desc'), parent, ext, not children)
        for child in children:
            walk(child, key, ext)

    for chapter in root.findall('chapter'):
        raw = _text(chapter, 'name')
        if not raw:
            raise ValueError('Chapter without name')
        key = 'chapter:' + raw
        ext = _extensions(chapter, {})
        add(key, 'chapter', raw, _text(chapter, 'desc'), None)
        for section in chapter.findall('section'):
            raw = section.get('id') or _text(section, 'name')
            if not raw:
                raise ValueError('Section without ID')
            block = 'block:' + raw
            block_ext = _extensions(section, ext)
            add(block, 'block', raw, _text(section, 'desc'), key)
            for diagnosis in section.findall('diag'):
                walk(diagnosis, block, block_ext)
    if not nodes:
        raise ValueError(f'No hierarchy nodes in {path}')
    return nodes


def _code_titles(path: Path, format_name: str) -> dict[str, str]:
    titles = {}
    with path.open(encoding='utf-8-sig') as handle:
        for number, line in enumerate(handle, 1):
            line = line.rstrip('\r\n')
            if not line.strip():
                continue
            if format_name == 'order':
                if len(line) < 77 or not line[:5].isdigit() or line[14] not in '01':
                    raise ValueError(f'Invalid official fixed-width order row {number}: {path}')
                if line[14] == '0':
                    continue
                code = normalize_code(line[6:13].strip())
                label = line[77:].strip() or line[16:76].strip()
            elif format_name == 'codes':
                parts = line.split(None, 1)
                if len(parts) != 2:
                    raise ValueError(f'Invalid code/title row {number}: {path}')
                code, label = normalize_code(parts[0]), parts[1].strip()
            else:
                raise ValueError(f'Unknown codes_format: {format_name}')
            if code in titles and titles[code] != label:
                raise ValueError(f'Conflicting official title for {code}')
            titles[code] = label
    if not titles:
        raise ValueError(f'No billable code titles found in {path}')
    return titles


def _add_billable_codes(nodes, titles, release):
    # Index only explicit terminal nodes. Intersect against the official valid
    # code list so inherited rules never fabricate nonexistent billable codes.
    candidates = defaultdict(list)
    for key, node in nodes.items():
        if node['kind'] != 'code' or not node['_leaf']:
            continue
        raw = normalize_code(node['code'])
        if len(raw) > 6:
            continue
        for char in node['_extensions']:
            candidates[raw.ljust(6, 'X') + char].append(key)
    unresolved = []
    for code, label in titles.items():
        key = 'code:' + code
        if key in nodes:
            nodes[key]['billable'] = True
            nodes[key]['billable_label'] = label
            continue
        parents = candidates.get(code, [])
        if not parents:
            unresolved.append(code)
            continue
        parent = max(parents, key=lambda p: len(normalize_code(nodes[p]['code'])))
        if len(parents) > 1:
            longest = len(normalize_code(nodes[parent]['code']))
            if sum(len(normalize_code(nodes[p]['code'])) == longest for p in parents) != 1:
                raise ValueError(f'Ambiguous extension parent for {code} in {release}')
        raw = code[:3] + '.' + code[3:]
        nodes[key] = {'kind': 'extension', 'code': raw, 'raw_code': raw, 'label': label,
                      'parents': [parent], 'materialize': False, 'billable': True,
                      'billable_label': label, '_leaf': True, '_extensions': {},
                      'extension_ancestor': parent,
                      'extension_evidence': 'Official billable code list; inherited seventh-character definition; X padding to position 7'}
    if unresolved:
        raise ValueError(f'{release}: {len(unresolved)} official billable codes lack an explicit tabular node or supported extension parent: {unresolved[:25]}')


def parse_references(manifest_path) -> dict[str, dict]:
    """Return source nodes with latest applicable hierarchy plus full history.

    Paths in the manifest are relative to the manifest's directory. Hashes are
    checked on every invocation. ``effective_to`` dates are inclusive. The
    manifest's scope.as_of defaults to 2021-12-31, so later releases are rejected.
    An amendment may use the same base XML/codes plus a separate pinned patch.
    """
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    sources = manifest['sources']
    cutoff = manifest.get('scope', {}).get('as_of', '2021-12-31')
    date.fromisoformat(cutoff)
    merged = {}
    seen_releases = set()
    for source in sorted(sources, key=lambda item: (item['effective_from'], item['release'])):
        release = source['release']
        start, end = source['effective_from'], source['effective_to']
        date.fromisoformat(start)
        date.fromisoformat(end)
        if start > end or release in seen_releases:
            raise ValueError(f'Invalid/repeated release: {release}')
        seen_releases.add(release)
        if start > cutoff or start > '2021-12-31' or end < '2016-01-01':
            raise ValueError(f'Release outside the 2016–2021 claim interval: {release}')
        xml_path = _verified(manifest_path.parent, source, 'xml_path', 'xml_sha256')
        nodes = _parse_xml(xml_path)
        code_path = _verified(manifest_path.parent, source, 'codes_path', 'codes_sha256')
        titles = _code_titles(code_path, source.get('codes_format', 'codes'))
        if source.get('patch_path'):
            patch_path = _verified(manifest_path.parent, source, 'patch_path', 'patch_sha256')
            _verified(manifest_path.parent, source, 'amendment_path', 'amendment_sha256')
            patch = _parse_xml(patch_path)
            for key, node in patch.items():
                if key in nodes:
                    raise ValueError(f'Additive patch conflicts with existing node {key}')
                nodes[key] = node
            for code, label in source.get('patch_billable_codes', {}).items():
                titles[normalize_code(code)] = label
        _add_billable_codes(nodes, titles, release)
        for key, node in nodes.items():
            observation = {'release': release, 'effective_from': start, 'effective_to': end,
                           'label': node['label'], 'parents': node['parents'], 'kind': node['kind'],
                           'billable': node['billable'], 'source_url': source['url'],
                           'xml_path': source['xml_path'], 'xml_sha256': source['xml_sha256']}
            if node.get('billable_label'):
                observation['billable_label'] = node['billable_label']
            if source.get('patch_path') and key in patch:
                observation.update(source_url=source['amendment_url'], patch_path=source['patch_path'],
                                   patch_sha256=source['patch_sha256'], derived_from_pdf_page=1)
            old = merged.get(key)
            history = old['release_provenance'] if old else []
            history.append(observation)
            current = {k: v for k, v in node.items() if not k.startswith('_')}
            current.update(selected_release=release, selected_effective_from=start,
                           source_releases=[r['release'] for r in history], release_provenance=history,
                           billable_releases=[r['release'] for r in history if r['billable']])
            merged[key] = current
    if not merged:
        raise ValueError('No applicable reference nodes')
    for key, node in merged.items():
        history = node['release_provenance']
        labels = {r['label'] for r in history}
        parents = {tuple(r['parents']) for r in history}
        node['conflicts'] = []
        for kind, values in [('label', labels), ('parents', parents)]:
            if len(values) > 1:
                node['conflicts'].append({'type': kind, 'selected_release': node['selected_release'],
                                          'observations': [{'release': r['release'], 'value': r[kind]} for r in history]})
        for parent in node['parents']:
            if parent not in merged:
                raise ValueError(f'Missing reference parent {parent} for {key}')
    _check_acyclic(merged)
    return merged


def _check_acyclic(nodes):
    state = {}
    def visit(key):
        if state.get(key) == 1:
            raise ValueError(f'Cycle in selected reference hierarchy at {key}')
        if state.get(key) == 2:
            return
        state[key] = 1
        for parent in nodes[key]['parents']:
            visit(parent)
        state[key] = 2
    for key in nodes:
        visit(key)


def reference_conflicts(nodes):
    """Return structured label/parent changes without adding old edges."""
    return [{'node_key': key, **conflict} for key, node in sorted(nodes.items()) for conflict in node['conflicts']]


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest')
    args = parser.parse_args()
    nodes = parse_references(args.manifest)
    counts = defaultdict(int)
    for node in nodes.values():
        counts[node['kind']] += 1
    print(json.dumps({'nodes': len(nodes), 'kinds': dict(counts), 'conflicts': len(reference_conflicts(nodes))}, indent=2))
