"""Автозапуск сайта и плановое обновление Moba на пользовательском Mac.

python -m scripts.install_macos_services [--install]
Без --install только готовит plist в deploy/macos/generated.
"""
import argparse
import os
import plistlib
import subprocess
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--install',action='store_true');args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    logs=root/'var/log';logs.mkdir(parents=True,exist_ok=True)
    generated=root/'deploy/macos/generated';generated.mkdir(parents=True,exist_ok=True)
    python=str(root/'.venv/bin/python')
    entries={
      'com.samaraparts.web':dict(ProgramArguments=['/usr/bin/caffeinate','-i',python,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8000','--proxy-headers','--forwarded-allow-ips','127.0.0.1'],KeepAlive=True,RunAtLoad=True,ThrottleInterval=10),
      'com.samaraparts.prices':dict(ProgramArguments=[python,'run_parser.py','--apply'],StartCalendarInterval=[dict(Hour=h,Minute=15) for h in (8,12,16,20)]),
      'com.samaraparts.onec':dict(ProgramArguments=[python,'run_onec_sync.py','--apply'],RunAtLoad=True,StartInterval=300),
    }
    for label,options in entries.items():
        value=dict(Label=label,WorkingDirectory=str(root),ProcessType='Background',
                   EnvironmentVariables={'PYTHONUNBUFFERED':'1','PATH':'/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin'},
                   StandardOutPath=str(logs/f'{label}.log'),StandardErrorPath=str(logs/f'{label}.error.log'),**options)
        path=generated/f'{label}.plist';path.write_bytes(plistlib.dumps(value))
        if args.install:
            target=Path.home()/'Library/LaunchAgents'/path.name
            if target.exists() and target.read_bytes()!=path.read_bytes():
                raise SystemExit(f'Существующий LaunchAgent отличается: {target}; сначала проверьте его')
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(path.read_bytes())
            domain=f'gui/{os.getuid()}'
            if subprocess.run(['launchctl','print',f'{domain}/{label}'],capture_output=True).returncode:
                subprocess.run(['launchctl','bootstrap',domain,str(target)],check=True)
        print(('INSTALLED ' if args.install else 'PREPARED ')+label)

if __name__=='__main__':main()
