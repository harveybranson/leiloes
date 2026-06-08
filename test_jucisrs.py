import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from scraper_jucisrs import fetch_jucisrs_regulares

regulares = fetch_jucisrs_regulares()
com_site = [l for l in regulares if l.get('site')]
sem_site = [l for l in regulares if not l.get('site')]
print(f'Total: {len(regulares)} | Com site: {len(com_site)} | Sem site: {len(sem_site)}')

print('\nPRIMEIROS 15 COM SITE:')
for l in com_site[:15]:
    mat = l['matricula']
    nome = l['nome'][:36]
    site = l['site']
    print(f'  [{mat:>3}] {nome:<36} | {site}')

print('\nPRIMEIROS 5 SEM SITE:')
for l in sem_site[:5]:
    mat = l['matricula']
    nome = l['nome'][:40]
    email = l['email'][:40]
    print(f'  [{mat:>3}] {nome:<40} | {email}')
