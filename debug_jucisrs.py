import requests, urllib3, re
from bs4 import BeautifulSoup
urllib3.disable_warnings()
sess = requests.Session()
sess.headers.update({'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0'})
sess.get('https://sistemas.jucisrs.rs.gov.br/leiloeiros/', timeout=15, verify=False)
r = sess.post('https://sistemas.jucisrs.rs.gov.br/leiloeiros/busca/listar',
    data={'Nome': '', 'CodMunicipio': '0'}, timeout=20, verify=False)
r.encoding = 'latin-1'
html = r.text

# Save for manual inspection
with open('jucisrs_raw.html', 'w', encoding='utf-8', errors='replace') as f:
    f.write(html)
print('Saved HTML. Size:', len(html))

# Look for patterns
for p in ['Regular', 'REGULAR', 'Matricula', 'matricula', 'Site:', 'LEILOEIRO', 'leiloeiro', 'Situacao', 'Situação']:
    idx = html.find(p)
    if idx > 0:
        print(f'\nPattern "{p}" at {idx}:')
        print(html[max(0,idx-50):idx+150])
        break

# Show different sections
print('\n--- Section 30000:30500 ---')
print(html[30000:30500])

# Find any links to leiloeiro details
soup = BeautifulSoup(html, 'html.parser')
links = [a['href'] for a in soup.find_all('a', href=True) if 'leil' in a['href'].lower() or 'detalhe' in a['href'].lower() or 'ver' in a['href'].lower()]
print('\nLeiloeiro links found:', links[:10])

# All divs with non-empty class
for d in soup.find_all('div', class_=True):
    cls = d.get('class', [])
    txt = d.get_text(strip=True)
    if len(txt) > 200 and len(txt) < 5000 and cls:
        print('\nDIV class:', cls, 'len:', len(txt))
        print(txt[:300])
        break
