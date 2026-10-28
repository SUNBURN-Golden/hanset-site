import shutil
import subprocess
import tempfile
from pathlib import Path
from ..util.pii import strip_pii
NAME='pdf'
VERSION='1.0.0'

def extract(content: bytes, url: str = '') -> dict:
    if not shutil.which('pdftotext'):
        return dict(extractor=NAME,extractor_version=VERSION,status='unavailable',text='',pages=[])
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/'input.pdf'
        path.write_bytes(content)
        result=subprocess.run(['pdftotext','-layout',str(path),'-'],capture_output=True,timeout=30,check=True)
    pages=strip_pii(result.stdout.decode('utf-8',errors='replace')).split('\f')
    if pages and not pages[-1].strip():
        pages.pop()
    return dict(extractor=NAME,extractor_version=VERSION,status='ok',pages=[{'page':n,'text':t} for n,t in enumerate(pages,1)],text='\n'.join(f'[page {n}]\n{t}' for n,t in enumerate(pages,1)))
