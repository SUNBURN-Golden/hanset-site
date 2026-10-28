from hanset_active.util.pii import strip_pii
from hanset_active.util.hashing import normalize_text, content_hash

def test_sanitizes_text_url_and_bytes():
    # Construct invented sensitive forms at runtime; none are fixture file data.
    phone='0'+'10'+'-'+'1234'+'-'+'5678'
    email='fixture'+'@'+'example.test'
    token='a'*40
    value=f'{phone} {email} Bearer {token} https://venue-a.test/?token={token}'
    clean=strip_pii(value)
    assert phone not in clean and email not in clean and token not in clean
    assert all(marker in clean for marker in ('[phone]','[email]','[token]'))
    assert strip_pii(value.encode())==clean.encode()
    assert strip_pii('hanset@soulbound.co.kr')=='hanset@soulbound.co.kr'

def test_other_phone_formats_and_encoded_params():
    for parts in [('0','2','-','123','-','4567'),('+','82',' ','10',' ','1234',' ','5678'),('1','588','-','1234'),('+','1',' ','202',' ','555',' ','0123')]:
        assert '[phone]' in strip_pii(''.join(parts))
    assert strip_pii('https://venue-a.test/?auth%3D'+'b'*40).endswith('auth=[token]')

def test_hashing():
    assert normalize_text(' A\n B  ')== 'A B'
    assert content_hash('<main>Hello world</main><script>volatile()</script>','text/html')==content_hash('<main>Hello world</main>','text/html')
    assert content_hash(b'a','application/pdf')!=content_hash(b'b','application/pdf')

def test_json_assignments_and_jwt():
    import json
    short='invented-value'
    result=strip_pii(json.dumps({'token':short,'api_key':short,'secret':short}))
    assert short not in result
    assert json.loads(result)==dict(token='[token]',api_key='[token]',secret='[token]')
    jwt='eyJ'+'a'*12+'.'+'b'*12+'.'+'c'*12
    assert strip_pii(jwt)=='[token]'
