#!/usr/bin/env python3
"""从已验证Catalog候选签发不可覆盖的Capability，并只归档ZIP与Manifest。"""
from __future__ import annotations
import argparse, json, shutil, tempfile
from datetime import datetime
from pathlib import Path
from build_skill import write_zip, verify_source_snapshot
from verify_skill import content_tree_entries, tree_hash, _module_hashes, verify_skill, verify_zip

PUBLISHER = "OptionHelper Project Team"
def sign(candidate: Path, release_root: Path, published_at: str) -> Path:
    candidate=candidate.resolve(); release_root=release_root.resolve(); datetime.fromisoformat(published_at)
    archive = release_root / "option-helper.zip"
    manifest_target = release_root / "capability-manifest.json"
    if archive.exists() or manifest_target.exists(): raise FileExistsError(f"Capability版本已存在：{release_root}")
    if verify_skill(candidate) or verify_source_snapshot(candidate): raise ValueError("候选未通过签发前验收")
    manifest=json.loads((candidate/'capability-manifest.json').read_text())
    catalog_version = manifest.get('catalog_version')
    if (
        not isinstance(catalog_version, str)
        or release_root.name != catalog_version
        or manifest.get('release_status') != 'candidate_from_published_catalog'
    ):
        raise ValueError("Capability必须绑定同版本的正式Catalog候选")
    with tempfile.TemporaryDirectory(prefix='capability-sign-') as temp:
        staged=Path(temp)/'option-helper'; shutil.copytree(candidate,staged)
        entries=content_tree_entries(staged); data=json.loads((staged/'capability-manifest.json').read_text())
        data.update({'package_status':'published','capability_version':catalog_version,'release_status':'published','formal_release':True,'execution_scope':'production','design_system_version':'v1.0','published_by':PUBLISHER,'published_at':published_at,'content_tree_entries':entries,'content_hashes':{x['path']:x['sha256'] for x in entries},'content_tree_hash':tree_hash(entries),'module_content_hashes':_module_hashes(entries)})
        (staged/'capability-manifest.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
        if verify_skill(staged): raise ValueError('签发后Manifest验收失败')
        archive=write_zip(staged)
        if verify_zip(archive): raise ValueError('签发ZIP解压复验失败')
        release_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(archive), str(release_root / "option-helper.zip"))
        shutil.copy2(staged / "capability-manifest.json", manifest_target)
    return release_root / "option-helper.zip"
def main():
 p=argparse.ArgumentParser();p.add_argument('--candidate',type=Path,required=True);p.add_argument('--release-root',type=Path,required=True);p.add_argument('--published-at',required=True);a=p.parse_args();print(sign(a.candidate,a.release_root,a.published_at))
if __name__=='__main__': main()
