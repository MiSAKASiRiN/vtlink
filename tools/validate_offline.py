"""Reproduce package validation without opening USB/serial or changing hardware."""
import argparse
import importlib.metadata
import json
import os
import pathlib
import platform
import re
import subprocess
import sys
from urllib.parse import unquote

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=pathlib.Path, default=ROOT/'work/offline-validation')
    args = ap.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    command_output = output.relative_to(ROOT) if output.is_relative_to(ROOT) else output
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    environment = {'python':sys.version, 'platform':platform.platform(), 'packages':{}}
    for name in ('av','pyserial','pyusb','Pillow'):
        try:environment['packages'][name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:environment['packages'][name]=None
    (output/'environment.json').write_text(json.dumps(environment,indent=2)+'\n')
    tasks = [
        ('sdk', ['-m','unittest','discover','-s','tests','-v']),
        ('h264',['tools/decode_h264_sample.py',
                 'samples/vt12-startup.udpbin',
                 '--output',str(command_output/'h264')]),
        ('hevc',['tools/decode_hevc_sample.py',
                 'samples/vt13-video.h265','--snapshot',str(command_output/'hevc.png')]),
        ('frames-vt12',['-m','vtlink','frames','--family','vt12']),
        ('frames-vt13',['-m','vtlink','frames','--family','vt13']),
        ('example',['examples/offline_packets.py']),
    ]
    for file in ('tools/vt12_video.py','tools/vt12_macos_rndis.py','tools/macos_preview.py',
                 'tools/macos_probe.py','tools/capture_vt13.py','examples/serial_monitor.py'):
        tasks.append(('help-'+pathlib.Path(file).stem,[file,'--help']))
    results=[]
    for name, command in tasks:
        try:
            proc=subprocess.run([sys.executable,*command],cwd=ROOT,env=env,capture_output=True,text=True,timeout=120)
            log=proc.stdout+proc.stderr
            passed=proc.returncode==0
            # Decoders' successful exit alone is insufficient to validate image recovery.
            if name=='h264' and passed:
                d=json.loads(proc.stdout)
                passed=(d.get('decoded_frames')==297 and d.get('key_frames',0)>=1
                        and d.get('decode_exceptions')==0 and d.get('corrupt_frames')==0
                        and '1280x720' in d.get('dimensions',{}))
            elif name=='hevc' and passed:
                d=json.loads(proc.stdout)
                passed=(d.get('synced') and d.get('frames_returned',0)>=59
                        and d.get('resolution')==[1920,1080] and d.get('decode_faults')==0)
            results.append(dict(name=name,command=command,returncode=proc.returncode,passed=bool(passed),log=name+'.log'))
        except (subprocess.TimeoutExpired,ValueError) as exc:
            log=str(exc);results.append(dict(name=name,command=command,passed=False,log=name+'.log'))
        (output/(name+'.log')).write_text(log,encoding='utf-8')
        print(name, 'PASS' if results[-1]['passed'] else 'FAIL',flush=True)
    broken=[]
    for path in ROOT.rglob('*.md'):
        if any(p in ('work','.venv') for p in path.relative_to(ROOT).parts):continue
        for match in re.finditer(r'\[[^\]]*\]\(([^)]+)\)',path.read_text(encoding='utf-8')):
            link=match.group(1).strip('<>')
            if '://' in link or link.startswith(('#','mailto:')):continue
            target=unquote(link.split('#',1)[0])
            resolved = (path.parent/target).resolve()
            if target and not resolved.exists() and resolved != output/'report.json':
                broken.append({'file':str(path.relative_to(ROOT)),'link':link})
    results.append(dict(name='documentation-links',passed=not broken,broken=broken))
    report={'hardware_access':False,'passed':all(r['passed'] for r in results),
            'checks':results,'environment':'environment.json',
            'scope':'Offline software and saved sample validation only; no new hardware validation'}
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':report['passed'],'checks':len(results),'broken_links':broken},ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
