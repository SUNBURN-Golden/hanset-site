"""Offline fixtures: a configurable threaded server and generated documents."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import io
import json
import threading
import pytest
from PIL import Image, ImageDraw, ImageFont
from hanset_active.config import Settings
from hanset_active.db import connect
from hanset_active.fetcher import Fetcher
from hanset_active.util.timeutil import FakeClock

FIXTURES=Path(__file__).parent/'fixtures'

def minimal_pdf() -> bytes:
    stream=b'BT /F1 22 Tf 50 720 Td (Day pass 25000 KRW) Tj ET'
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    data=b'%PDF-1.4\n'; offsets=[0]
    for n,obj in enumerate(objects,1):
        offsets.append(len(data)); data+=f'{n} 0 obj\n'.encode()+obj+b'\nendobj\n'
    xref=len(data)
    data+=f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    for offset in offsets[1:]:
        data+=f'{offset:010d} 00000 n \n'.encode()
    return data+f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()

@pytest.fixture(scope='session')
def documents():
    image=Image.new('RGB',(1000,400),'white')
    draw=ImageDraw.Draw(image)
    try:
        font=ImageFont.truetype('DejaVuSans.ttf',48)
    except OSError:
        font=ImageFont.load_default(size=48)
    draw.text((40,50),'DAY PASS PRICE',font=font,fill='black')
    draw.text((40,150),'25000 KRW',font=font,fill='black')
    out=io.BytesIO(); image.save(out,format='PNG')
    return {'pdf':minimal_pdf(),'png':out.getvalue()}

@pytest.fixture
def server():
    routes={'/robots.txt':(200,{'Content-Type':'text/plain'},b'User-agent: *\nDisallow: /private\n')}
    log=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            log.append({'path':self.path,'headers':dict(self.headers)})
            route=routes.get(self.path,(404,{'Content-Type':'text/plain'},b'not found'))
            status,headers,body=route() if callable(route) else route
            if isinstance(body,str):
                body=body.encode()
            self.send_response(status)
            for k,v in headers.items():
                self.send_header(k,v)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):
                pass
        def log_message(self,*args):
            pass
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=http.serve_forever,daemon=True); thread.start()
    class Server:
        base=f'http://127.0.0.1:{http.server_port}'
        def add(self,path,body,status=200,headers=None):
            routes[path]=(status,headers or {'Content-Type':'text/html; charset=utf-8'},body)
    result=Server(); result.routes=routes; result.log=log
    try:
        yield result
    finally:
        http.shutdown(); http.server_close(); thread.join()

@pytest.fixture
def bot(tmp_path):
    settings=Settings(data_dir=tmp_path/'external',testing=True,allow_private_hosts=True,min_interval=0,global_gap=0)
    db=connect(settings.data_dir/'test.sqlite3')
    result=Fetcher(settings,db,FakeClock())
    yield result
    result.close(); db.close()

@pytest.fixture(autouse=True)
def isolate_qwen_key(monkeypatch, tmp_path):
    # The host may carry a real Qwen key file; tests must never read or use it.
    from hanset_active.collect import credentials
    monkeypatch.setenv('HANSET_QWEN_KEY_FILE', str(tmp_path / 'absent-qwen-key'))
    credentials._read_key.cache_clear()
    yield
    credentials._read_key.cache_clear()


@pytest.fixture(autouse=True)
def forbid_external_requests(monkeypatch):
    # Fail fast on any accidental internet attempt, including direct requests.
    import socket
    original=socket.socket.connect
    def connect_local(sock,address):
        if isinstance(address,tuple) and address[0] not in ('127.0.0.1','::1'):
            raise AssertionError('Offline test attempted external network')
        return original(sock,address)
    monkeypatch.setattr(socket.socket,'connect',connect_local)
