"""Fetch the pinned official CDC ICD-10-CM sources used by this project."""
import concurrent.futures
import hashlib
import json
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REF = ROOT / 'references'
BASE = 'https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Publications/ICD10CM/'
ASSETS = {
 '2016_xml': BASE+'2016/ICD10CM_FY2016_%20Full_%20XML.ZIP',
 '2016_codes': BASE+'2016/ICD10CM_FY2016_code_descriptions.zip',
 '2017_xml': BASE+'2017/ICD10CM_FY2017_Full_XML.zip',
 '2017_codes': BASE+'2017/icd10cm_codes_2017.txt',
 '2018_codes': BASE+'2018/2018-ICD-10-CM-Codes-File.zip',
 '2018_xml': BASE+'2018/ICD-10-CM-Codes-Tables-and-Index-2018.zip',
 '2019_xml': BASE+'2019/icd10cm_tabular_2019.xml',
 '2019_codes': BASE+'2019/icd10cm_codes_2019.txt',
 '2020_xml': BASE+'2020/icd10cm_tabular_2020.xml',
 '2020_codes': BASE+'2020/icd10cm_codes_2020.txt',
 '2020_apr_addenda': 'https://www.cdc.gov/nchs/media/pdfs/2024/06/ICD-10-CM-April-1-2020-addenda.pdf',
 '2021_xml': BASE+'2021/icd10cm_tabular_2021.xml',
 '2021_codes': BASE+'2021/icd10cm_codes_2021.txt',
 '2021_jan_xml': BASE+'2021/icd10cm-Full-Tab-Index%20Table-Jan-2021.zip',
 '2021_jan_codes': BASE+'2021/icd10cm-codes-order-Jan-2021.zip',
 '2022_xml': BASE+'2022/Table%20and%20Index%20zip.zip',
 '2022_codes': BASE+'2022/Code%20Descriptions%20zip.zip',
}

def sha(path):
 return hashlib.sha256(path.read_bytes()).hexdigest()

def fetch(item, expected_hashes=None):
 key,url=item
 suffix=Path(urllib.request.url2pathname(url.split('/')[-1])).suffix.lower()
 dest=REF/'downloads'/(key+suffix)
 if not dest.exists():
  error=None
  for attempt in range(3):
   try:
    with urllib.request.urlopen(url,timeout=45) as response:
     data=response.read()
    if not data or data[:20].lower().startswith(b'<html'):
     raise ValueError('Unexpected empty or HTML response')
    dest.write_bytes(data)
    error=None
    break
   except Exception as exc: error=exc
  if error: raise RuntimeError(f'{key}: {error}')
 if expected_hashes and url in expected_hashes and sha(dest)!=expected_hashes[url]:
  raise ValueError(f'{key}: downloaded bytes differ from the pinned manifest; original reference snapshots are required')
 print('downloaded',key,dest.stat().st_size,flush=True)
 result={'url':url,'download_path':str(dest.relative_to(ROOT)),'download_sha256':sha(dest)}
 if suffix=='.zip':
  result['members']=[]
  with zipfile.ZipFile(dest) as archive:
   for name in archive.namelist():
    low=name.lower()
    if ('tabular' in low and low.endswith('.xml')) or (any(term in low for term in ('codes', 'order')) and 'addenda' not in low and low.endswith('.txt')):
     out=REF/(key+'__'+Path(name).name.replace(' ','_'))
     out.write_bytes(archive.read(name))
     result['members'].append({'archive_member':name,'path':str(out.relative_to(ROOT)),'sha256':sha(out)})
 else:
  out=REF/(key+suffix)
  out.write_bytes(dest.read_bytes())
  result['members']=[{'path':str(out.relative_to(ROOT)),'sha256':sha(out)}]
 return key,result

def prepare_manifest(assets):
 """Select only the snapshots needed for services in calendar 2016–2021."""
 def member(key,ending):
  matches=[m for m in assets[key]['members'] if m['path'].lower().endswith(ending)]
  if ending=='.txt':
   preferred=[m for m in matches if 'codes' in Path(m['path']).name.lower() and 'order' not in Path(m['path']).name.lower()]
   matches=preferred or matches
  if len(matches)!=1: raise ValueError(f'Ambiguous source member {key}: {matches}')
  return matches[0]
 def entry(release,start,end,xml_key,codes_key):
  xml=member(xml_key,'.xml');code=member(codes_key,'.txt')
  return {'release':release,'effective_from':start,'effective_to':end,
   'url':assets[xml_key]['url'],'download_sha256':assets[xml_key]['download_sha256'],
   'xml_path':str(Path(xml['path']).relative_to('references')),'xml_sha256':xml['sha256'],
   'archive_member':xml.get('archive_member'),
   'codes_url':assets[codes_key]['url'],'codes_download_sha256':assets[codes_key]['download_sha256'],
   'codes_path':str(Path(code['path']).relative_to('references')),'codes_sha256':code['sha256'],
   'codes_archive_member':code.get('archive_member'),
   'codes_format':'order' if 'order' in Path(code['path']).name.lower() else 'codes'}
 entries=[]
 for year in range(2016,2021):
  end=f'{year}-09-30' if year!=2020 else '2020-03-31'
  entries.append(entry(f'FY{year}',f'{year-1}-10-01',end,f'{year}_xml',f'{year}_codes'))
 patch_xml='''<?xml version="1.0" encoding="UTF-8"?>
<!-- Derived transcription of NEW hierarchy entries on page 1 of the official CDC
April 1, 2020 addenda. This is a local patch, not a CDC-published full XML file.
Exclusion and coding-instruction amendments are outside this hierarchy-only scope. -->
<ICD10CM.tabular><version>2020</version><chapter><name>22</name>
<desc>Codes for special purposes (U00-U85)</desc>
<section id="U00-U49"><desc>Provisional assignment of new diseases of uncertain etiology or emergency use</desc>
<diag><name>U07</name><desc>Emergency use of U07</desc>
<diag><name>U07.0</name><desc>Vaping-related disorder</desc></diag>
<diag><name>U07.1</name><desc>COVID-19</desc></diag>
</diag></section></chapter></ICD10CM.tabular>
'''
 patch_path=REF/'2020_april_derived_hierarchy_patch.xml';patch_path.write_text(patch_xml,encoding='utf-8')
 patch=entry('FY2020-April','2020-04-01','2020-09-30','2020_xml','2020_codes')
 pdf=member('2020_apr_addenda','.pdf')
 patch.update(base_release='FY2020',patch_path=patch_path.name,patch_sha256=sha(patch_path),
  amendment_url=assets['2020_apr_addenda']['url'],amendment_path=str(Path(pdf['path']).relative_to('references')),
  amendment_sha256=pdf['sha256'],patch_billable_codes={'U070':'Vaping-related disorder','U071':'COVID-19'},
  note='Base FY2020 tabular XML plus a local transcription of new hierarchy nodes from the official April 2020 addenda page 1. This is a derived hierarchy snapshot, not a separately published full XML release.')
 entries.append(patch)
 entries.append(entry('FY2021','2020-10-01','2020-12-31','2021_xml','2021_codes'))
 entries.append(entry('FY2021-January','2021-01-01','2021-09-30','2021_jan_xml','2021_jan_codes'))
 entries.append(entry('FY2022-October2021','2021-10-01','2022-03-31','2022_xml','2022_codes'))
 entries[-1]['note']='Original June 2021 Table and Index/Code Descriptions archives for October 1, 2021. The separate 2022 XML/TXT files on the CDC directory are April 2022 updates and are deliberately excluded.'
 manifest={'schema_version':1,'scope':{'calendar_year_start':2016,'calendar_year_end':2021,'as_of':'2021-12-31'},
  'source_agency':'CDC/NCHS','retrieved_on':'2026-09-24',
  'effective_date_reference':'https://www.cdc.gov/nchs/icd/icd-10-cm/files.html',
  'policy':'Select the newest applicable label and immediate-parent assertion for each node. Preserve all source release observations. Never merge old parent edges into the selected hierarchy.',
  'sources':entries}
 (REF/'sources.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
 return manifest

if __name__=='__main__':
 import argparse
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--verify', action='store_true', help='Verify local manifest hashes and parse references without network access')
 args=parser.parse_args()
 if args.verify:
  from icd_reference import parse_references, reference_conflicts
  nodes=parse_references(REF/'sources.json')
  print(json.dumps({'nodes':len(nodes),'conflicts':len(reference_conflicts(nodes)), 'verification':'passed'}, indent=2))
  raise SystemExit(0)
 (REF/'downloads').mkdir(parents=True,exist_ok=True)
 # Refuse a silent change of historical source bytes when rerunning this repo.
 expected_hashes={}
 if (REF/'sources.json').exists():
  pinned=json.loads((REF/'sources.json').read_text(encoding='utf-8'))
  for source in pinned['sources']:
   for url_key,hash_key in [('url','download_sha256'),('codes_url','codes_download_sha256'),('amendment_url','amendment_sha256')]:
    if source.get(url_key) and source.get(hash_key):
     expected_hashes[source[url_key]]=source[hash_key]
 assets={}
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
  for future in concurrent.futures.as_completed([pool.submit(fetch,item,expected_hashes) for item in ASSETS.items()]):
   key,result=future.result()
   assets[key]=result
   (REF/'assets.json').write_text(json.dumps(assets,indent=2)+'\n')
 prepare_manifest(assets)
 print('DONE',len(assets),flush=True)
