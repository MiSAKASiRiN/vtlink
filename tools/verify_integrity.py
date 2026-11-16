"""Verify every delivered file and report unlisted files, excluding runtime output."""
import hashlib
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def deliverable(path):
    rel = path.relative_to(ROOT)
    return (path.is_file() and not any(p in ('.git','work','.venv','build','dist') for p in rel.parts)
            and '__pycache__' not in rel.parts and not path.name.endswith('.pyc')
            and not any(p.endswith('.egg-info') for p in rel.parts)
            and rel.as_posix() != 'MANIFEST.sha256.json' and path.name != '.DS_Store')


def main():
    manifest = json.loads((ROOT/'MANIFEST.sha256.json').read_text())
    failures=[]
    for item in manifest['files']:
        path = (ROOT/item['path']).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():failures.append(item['path']+': missing/unsafe')
        elif path.stat().st_size != item['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:
            failures.append(item['path']+': modified')
    known={item['path'] for item in manifest['files']}
    extra=sorted(p.relative_to(ROOT).as_posix() for p in ROOT.rglob('*') if deliverable(p) and p.relative_to(ROOT).as_posix() not in known)
    result=dict(checked=len(known),failures=failures,unlisted=extra)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return int(bool(failures or extra))


if __name__ == '__main__':
    raise SystemExit(main())
