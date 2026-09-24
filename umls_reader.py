"""Strict, streaming readers for a locally licensed UMLS MRCONSO snapshot.

Only standard-library modules are used. ZIP members are opened in place and are
never extracted to disk. ``collect_anchors`` makes the first complete scan;
``iter_scoped_atoms`` makes the second scan and can feed SQLite one atom at a time.

An exact ICD10CM code atom supplies a CUI candidate, not ontology equivalence.
Multiple CUIs for one ICD code remain ambiguous and are never merged. Preferred
terms require the conjunction TS=P, STT=PF, ISPREF=Y. A caller must keep its
original ICD label when there are zero or multiple distinct preferred strings.

Format and flag references:
https://www.ncbi.nlm.nih.gov/books/NBK9685/
https://www.ncbi.nlm.nih.gov/books/NBK9685/table/ch03.T.concept_names_and_sources_file_mr/
https://lhncbc.nlm.nih.gov/LSG/Projects/lexicon/current/docs/designDoc/UDF/synonyms/synonymCan.html
"""
from __future__ import annotations

import gzip
import hashlib
import io
import re
import zipfile
import zlib
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Iterator

MRCONSO_FIELDS = (
    'CUI', 'LAT', 'TS', 'LUI', 'STT', 'SUI', 'ISPREF', 'AUI', 'SAUI',
    'SCUI', 'SDUI', 'SAB', 'TTY', 'CODE', 'STR', 'SRL', 'SUPPRESS', 'CVF',
)
_ICD = re.compile(r'[A-Z][0-9][A-Z0-9](?:\.?[A-Z0-9]{1,4})?\Z')


def normalize_icd_code(value: str) -> str:
    """Normalize an exact ICD code or range selector; never truncate.

    Ranges retain their ASCII hyphen and both complete endpoints. They are
    matched as literal source CODE values, never expanded into member codes.
    Chapter names/numbers are not converted into a code or range.
    """
    raw = str(value).strip().upper()
    endpoints = raw.split('-')
    if len(endpoints) not in {1, 2} or any(not _ICD.fullmatch(part) for part in endpoints):
        raise ValueError(f'Invalid ICD-10-CM code or exact range selector: {value!r}')
    return '-'.join(part.replace('.', '') for part in endpoints)


class MRConsoSource:
    """A repeatable, strictly validated MRCONSO stream with file provenance.

    ``progress`` optionally receives plain-text messages, including progress
    every ``progress_every`` physical rows (default one million). SHA256 is of
    the supplied file, so for ZIP/gzip it hashes the compressed input itself.
    ``release`` is user-supplied snapshot metadata; MRCONSO alone cannot prove
    the release name or the underlying ICD vocabulary's effective dates.
    """

    def __init__(self, path, member=None, release='2026AA', expected_sha256=None,
                 progress: Callable[[str], None] | None = None,
                 progress_every=1_000_000):
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file() or not self.path.stat().st_size:
            raise ValueError(f'Missing or empty MRCONSO input: {self.path}')
        self.release = str(release).strip()
        if not self.release or any(char in self.release for char in '\r\n\t'):
            raise ValueError('An explicit UMLS snapshot release identifier is required')
        if type(progress_every) is not int or progress_every < 1:
            raise ValueError('progress_every must be a positive integer')
        self.progress = progress
        self.progress_every = progress_every
        self.scans_started = 0
        self.scans_completed = 0
        self.last_scan_rows = 0
        self.member = None
        self._member_info = None
        self._initial_stat = self._stat_signature()
        with self.path.open('rb') as handle:
            magic = handle.read(4)
        if zipfile.is_zipfile(self.path):
            self.format = 'zip'
            with zipfile.ZipFile(self.path) as archive:
                candidates = [entry for entry in archive.infolist()
                              if not entry.is_dir() and self._is_mrconso_name(entry.filename)]
                if member is None:
                    if len(candidates) != 1:
                        names = [entry.filename for entry in candidates]
                        raise ValueError(f'ZIP must contain one unambiguous MRCONSO.RRF member; found {len(names)}: {names}. Select the exact member explicitly.')
                    selected = candidates[0]
                else:
                    matches = [entry for entry in candidates if entry.filename == member]
                    if len(matches) != 1:
                        raise ValueError(f'Selected ZIP member must identify exactly one MRCONSO.RRF entry: {member!r}')
                    selected = matches[0]
                # Duplicate exact member names are unsafe even if the caller
                # supplied that name; ZipFile would otherwise choose silently.
                if sum(entry.filename == selected.filename for entry in archive.infolist()) != 1:
                    raise ValueError(f'Duplicate ZIP member name: {selected.filename!r}')
                if selected.flag_bits & 1:
                    raise ValueError('Encrypted ZIP members are not supported')
                if selected.file_size == 0:
                    raise ValueError('Selected MRCONSO.RRF ZIP member is empty')
                self.member = selected.filename
                self._member_info = {'name': selected.filename, 'uncompressed_bytes': selected.file_size,
                                     'compressed_bytes': selected.compress_size,
                                     'crc32': f'{selected.CRC:08x}', 'compression_method': selected.compress_type}
        elif magic[:2] == b'\x1f\x8b':
            if member is not None:
                raise ValueError('ZIP member selection is valid only for ZIP inputs')
            self.format = 'gzip'
        elif self.path.suffix.lower() in {'.zip', '.gz', '.gzip'}:
            raise ValueError(f'Input extension indicates compression but its format is invalid: {self.path}')
        else:
            if member is not None:
                raise ValueError('ZIP member selection is valid only for ZIP inputs')
            self.format = 'rrf'
        self._emit(f'Hashing local {self.format.upper()} input for provenance: {self.path.name}')
        digest = hashlib.sha256()
        with self.path.open('rb') as handle:
            for block in iter(lambda: handle.read(4 * 1024 * 1024), b''):
                digest.update(block)
        self.sha256 = digest.hexdigest()
        if expected_sha256 is not None:
            expected = str(expected_sha256).lower()
            if not re.fullmatch(r'[0-9a-f]{64}', expected) or self.sha256 != expected:
                raise ValueError(f'MRCONSO input SHA256 does not match the expected value: {self.path}')
        self._assert_unchanged()

    @staticmethod
    def _is_mrconso_name(name):
        return PurePosixPath(name.replace('\\', '/')).name.casefold() == 'mrconso.rrf'

    def _stat_signature(self):
        stat = self.path.stat()
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def _assert_unchanged(self):
        if self._stat_signature() != self._initial_stat:
            raise ValueError('MRCONSO input changed during processing; start again with a stable snapshot')

    def _emit(self, text):
        if self.progress is not None:
            self.progress(text)

    @contextmanager
    def _text_stream(self):
        self._assert_unchanged()
        if self.format == 'zip':
            with zipfile.ZipFile(self.path) as archive:
                with archive.open(self.member, 'r') as binary:
                    with io.TextIOWrapper(binary, encoding='utf-8-sig', errors='strict', newline='') as text:
                        yield text
        elif self.format == 'gzip':
            with gzip.open(self.path, 'rt', encoding='utf-8-sig', errors='strict', newline='') as text:
                yield text
        else:
            with self.path.open('r', encoding='utf-8-sig', errors='strict', newline='') as text:
                yield text

    def iter_atoms(self) -> Iterator[dict]:
        """Yield all rows, failing on malformed rows instead of dropping them.

        Source strings are preserved exactly: no stripping, case folding,
        Unicode normalization, replacement decoding, or punctuation changes.
        UTF-8 BOM is accepted only at the beginning of the whole source stream.
        """
        self.scans_started += 1
        scan = self.scans_started
        self._emit(f'MRCONSO scan {scan}: starting')
        number = 0
        try:
            with self._text_stream() as stream:
                for number, line in enumerate(stream, 1):
                    # Strip only the physical record terminator, never STR.
                    if line.endswith('\n'):
                        line = line[:-1]
                        if line.endswith('\r'):
                            line = line[:-1]
                    elif line.endswith('\r'):
                        line = line[:-1]
                    fields = line.split('|')
                    where = f'{self.member or self.path.name}, row {number}'
                    if len(fields) != 19 or fields[-1] != '':
                        raise ValueError(f'Malformed MRCONSO record at {where}: expected 18 columns followed by a trailing pipe; found {len(fields) - 1 if fields and fields[-1] == "" else len(fields)} columns')
                    fields.pop()
                    atom = dict(zip(MRCONSO_FIELDS, fields))
                    if not re.fullmatch(r'C[0-9]+', atom['CUI']):
                        raise ValueError(f'Malformed MRCONSO CUI at {where}: {atom["CUI"]!r}')
                    if not atom['AUI'] or not atom['LAT'] or not atom['SAB'] or not atom['TTY'] or not atom['CODE'] or not atom['STR']:
                        raise ValueError(f'Malformed MRCONSO record at {where}: required AUI/LAT/SAB/TTY/CODE/STR field is empty')
                    if atom['TS'] not in {'P', 'S'} or atom['ISPREF'] not in {'Y', 'N'} or not atom['STT']:
                        raise ValueError(f'Malformed MRCONSO preference flags at {where}')
                    if atom['SUPPRESS'] not in {'N', 'O', 'E', 'Y'}:
                        raise ValueError(f'Malformed MRCONSO SUPPRESS flag at {where}: {atom["SUPPRESS"]!r}')
                    if any('\x00' in value or '\r' in value or '\n' in value for value in fields):
                        raise ValueError(f'Malformed MRCONSO control character at {where}')
                    atom['_row_number'] = number
                    atom['_member'] = self.member or self.path.name
                    if number % self.progress_every == 0:
                        self._emit(f'MRCONSO scan {scan}: processed {number:,} rows')
                    yield atom
        except UnicodeDecodeError as exc:
            raise ValueError(f'MRCONSO is not valid UTF-8 near row {number + 1}: {exc}') from exc
        except (zipfile.BadZipFile, gzip.BadGzipFile, EOFError, zlib.error) as exc:
            raise ValueError(f'Corrupt compressed MRCONSO input near row {number + 1}: {exc}') from exc
        if number == 0:
            raise ValueError('MRCONSO stream has no records')
        self._assert_unchanged()
        self.scans_completed += 1
        self.last_scan_rows = number
        self._emit(f'MRCONSO scan {scan}: complete, {number:,} rows')

    def provenance(self) -> dict:
        """Return serializable provenance without triggering an extra scan."""
        return {'source_path': str(self.path), 'filename': self.path.name,
                'format': self.format, 'member': self.member, 'member_details': self._member_info,
                'sha256': self.sha256, 'input_bytes': self._initial_stat[2],
                'umls_release': self.release, 'release_assertion': 'user-supplied snapshot metadata',
                'release_note': 'MRCONSO does not itself establish the source ICD vocabulary release or historical service-date validity.',
                'encoding': 'UTF-8', 'scans_started': self.scans_started,
                'scans_completed': self.scans_completed, 'last_scan_rows': self.last_scan_rows}


def collect_anchors(source: MRConsoSource, eligible_codes: Iterable[str]) -> dict:
    """Pass 1: exact ICD10CM CODE anchors for eligible official ICD nodes.

    A supplied code/range must have valid syntax. Only SAB=ICD10CM, LAT=ENG and
    SUPPRESS=N atoms are eligible. Official block/chapter ranges can match only
    the same complete CODE range, after case/decimal normalization. Ranges are
    never expanded, and a chapter number/name never supplies a code. Ambiguous
    CUI candidates and every matching source atom are preserved for audit,
    not collapsed or ranked. Missing range atoms remain explicitly unmapped.
    """
    codes = {normalize_icd_code(code) for code in eligible_codes}
    by_code = {code: {'status': 'unmapped', 'cuis': [], 'atoms': []} for code in sorted(codes)}
    stats = {'rows_scanned': 0, 'english_nonsuppressed_icd_atoms': 0,
             'non_diagnosis_icd_atoms': 0, 'ineligible_icd_atoms': 0, 'matched_anchor_atoms': 0,
             'eligible_codes': len(codes), 'eligible_ranges': sum('-' in code for code in codes),
             'matched_range_atoms': 0}
    for atom in source.iter_atoms():
        stats['rows_scanned'] += 1
        if atom['SAB'] != 'ICD10CM' or atom['LAT'] != 'ENG' or atom['SUPPRESS'] != 'N':
            continue
        stats['english_nonsuppressed_icd_atoms'] += 1
        try:
            code = normalize_icd_code(atom['CODE'])
        except ValueError:
            stats['non_diagnosis_icd_atoms'] += 1
            continue
        if code not in codes:
            stats['ineligible_icd_atoms'] += 1
            continue
        by_code[code]['atoms'].append(atom)
        stats['matched_anchor_atoms'] += 1
        if '-' in code:
            stats['matched_range_atoms'] += 1
    selected_cuis = set()
    for record in by_code.values():
        cuis = sorted({atom['CUI'] for atom in record['atoms']})
        record['cuis'] = cuis
        if len(cuis) == 1:
            record['status'] = 'unambiguous'
            selected_cuis.add(cuis[0])
        elif len(cuis) > 1:
            record['status'] = 'ambiguous'
    for status in ('unambiguous', 'ambiguous', 'unmapped'):
        stats[status + '_codes'] = sum(record['status'] == status for record in by_code.values())
    stats['selected_cuis'] = len(selected_cuis)
    return {'by_code': by_code, 'selected_cuis': selected_cuis, 'stats': stats}


def iter_scoped_atoms(source: MRConsoSource, cuis: Iterable[str]) -> Iterator[dict]:
    """Pass 2: stream English nonsuppressed atoms for selected CUIs only.

    All SABs are retained as source-attributed term evidence. The caller can
    write each atom directly to SQLite. No collection of the scoped terms is
    held in memory here. A CUI shared with an ambiguous ICD code may still be
    selected by a different unambiguous code; callers must assign terms only
    to each code's own unambiguous anchor record.
    """
    selected = set(cuis)
    if any(not isinstance(cui, str) or not re.fullmatch(r'C[0-9]+', cui) for cui in selected):
        raise ValueError('Scoped CUI set contains a malformed CUI')
    if not selected:
        return
    for atom in source.iter_atoms():
        if atom['CUI'] in selected and atom['LAT'] == 'ENG' and atom['SUPPRESS'] == 'N':
            yield atom


def is_preferred_atom(atom: dict) -> bool:
    """True only for an English nonsuppressed atom meeting all three flags."""
    return (atom.get('LAT') == 'ENG' and atom.get('SUPPRESS') == 'N'
            and atom.get('TS') == 'P' and atom.get('STT') == 'PF' and atom.get('ISPREF') == 'Y')


def preferred_strings(atoms: Iterable[dict]) -> list[str]:
    """Return exact distinct preferred strings; never break a preference tie."""
    return sorted({atom['STR'] for atom in atoms if is_preferred_atom(atom)})
