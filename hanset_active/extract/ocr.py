import csv
import io
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from PIL import Image
from ..util.pii import strip_pii
NAME='ocr'
VERSION='1.0.0'

def is_image_notice(content: bytes, filename: str = '', alt: str = '', context: str = '') -> bool:
    try:
        with Image.open(io.BytesIO(content)) as image:
            w,h=image.size
        return w>=400 and h>=200 and bool(re.search(r'price|요금|이용|시간표|timetable|menu|rate', ' '.join((filename,alt,context)), re.I))
    except (OSError,ValueError):
        return False

def extract(content: bytes, url: str = '', *, languages: str = 'kor+eng') -> dict:
    result=dict(extractor=NAME,extractor_version=VERSION,text='',mean_confidence=0.0,boxes=[],status='unavailable')
    if not shutil.which('tesseract'):
        return result
    available=subprocess.run(['tesseract','--list-langs'],capture_output=True,text=True,timeout=15).stdout.split()
    selected='+'.join(lang for lang in languages.split('+') if lang in available)
    if not selected:
        return result
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/'input.png'
        with Image.open(io.BytesIO(content)) as im:
            im.convert('RGB').save(path)
        for psm in (6,11):
            proc=subprocess.run(['tesseract',str(path),'stdout','-l',selected,'--psm',str(psm),'tsv'],capture_output=True,text=True,check=True,timeout=60)
            rows=[r for r in csv.DictReader(io.StringIO(proc.stdout),delimiter='\t') if (r.get('text') or '').strip() and float(r.get('conf',-1))>=0]
            confidence=sum(float(r['conf']) for r in rows)/len(rows) if rows else 0
            candidate=dict(result,status='ok',text=strip_pii(' '.join(r['text'] for r in rows)),mean_confidence=confidence,boxes=[dict(text=strip_pii(r['text']),left=int(r['left']),top=int(r['top']),width=int(r['width']),height=int(r['height']),confidence=float(r['conf'])) for r in rows],psm=psm,languages=selected)
            if confidence>=result['mean_confidence']:
                result=candidate
            if confidence>=60 and rows:
                break
    return result
