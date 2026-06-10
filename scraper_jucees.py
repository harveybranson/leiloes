"""
scraper_jucees.py
=================
Coleta imóveis de leiloeiros REGULAR credenciados pela JUCEES (ES).

Fluxo:
  1. Dados hardcoded dos PDFs anexos (Relação de Leiloeiros Regulares JUCEES)
     + scrape de https://leiloeiros.jucees.es.gov.br/ para atualizações
  2. Filtra apenas Regular (exclui cancelados/suspensos/irregulares)
  3. Deriva site do campo Site: ou do domínio do e-mail
  4. Visita cada site com requests → Playwright como fallback
  5. Salva CSV → csv/leiloeiros_jucees_YYYY-MM-DD.csv
                  csv/imoveis_jucees_YYYY-MM-DD.csv
  6. Importa para SQLite (imoveis_leiloeiros.db)
  7. Importa para PostgreSQL Docker (leilao_db)
  8. Relatório por leiloeiro a cada 5 min + relatório final de dificuldades

Uso:
  python scraper_jucees.py [--sem-banco] [--max-sites N] [--reset]
"""
import sys, io, os
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import re, csv, json, time, hashlib, sqlite3, argparse, threading
from pathlib import Path
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse, urljoin

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from bs4 import BeautifulSoup

# ── Configuração ───────────────────────────────────────────────────────────────
BASE          = Path(__file__).resolve().parent
CSV_DIR       = BASE / "csv"
DB_FILE       = BASE / "imoveis_leiloeiros.db"
PROGRESS_FILE = BASE / "scraper_jucees_progress.json"
LOG_FILE      = BASE / "scraper_jucees.log"

JUCEES_URL    = "https://leiloeiros.jucees.es.gov.br/"
TODAY         = datetime.now().strftime("%Y-%m-%d")

FIELDNAMES_IMOVEIS = [
    "id_externo","leiloeiro","leiloeiro_site","titulo","tipo_imovel","tipo_leilao",
    "estado","cidade","cep","endereco_completo",
    "valor_minimo","valor_avaliacao","area_total","quartos",
    "data_primeiro_leilao","data_segundo_leilao","data_encerramento",
    "url_original","imagem_principal","numero_processo","arquivos","descricao",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5",
}

# Estado global compartilhado entre threads
_lock   = threading.Lock()
_estado = {
    "imoveis": [],
    "por_leiloeiro": {},
    "sites_ok": 0,
    "sites_err": 0,
    "sites_sem_leilao": 0,
    "erros": [],
    "leiloeiro_atual": "",
    "inicio": datetime.now().isoformat(),
    "dificuldades": [],
}

# ── Logging ────────────────────────────────────────────────────────────────────
def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    linha = f"[{ts}] {msg}"
    print(linha)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except Exception:
        pass

# ── Leiloeiros REGULARES — dados dos PDFs JUCEES ───────────────────────────────
# Fonte: "Relação_Leiloeiros_Regulares.pdf" (JUCEES) +
#        "Leiloeiros Espírito Santo +.pdf" (site jucees)
# Somente leiloeiros com situação REGULAR; cancelados/suspensos excluídos.
LEILOEIROS_JUCEES = [
    {"matricula":"002/1976","nome":"DJANIR DA RÓS",
     "site":"https://www.djanirleiloes.com.br/",
     "email":"ddr.djanir@hotmail.com","telefone":"(27) 3229-9070",
     "cidade_leiloeiro":"Vila Velha","uf_leiloeiro":"ES"},
    {"matricula":"004/1978","nome":"ANTONIO FREIRE DE PAIVA ALMEIDA",
     "site":"https://www.publicjud.com.br",
     "email":"paivaleiloeiro@uol.com.br","telefone":"(27) 3315-1479",
     "cidade_leiloeiro":"Vitória","uf_leiloeiro":"ES"},
    {"matricula":"005/1978","nome":"ALEXANDRE BUAIZ NETO",
     "site":"https://www.buaizleiloes.com.br/",
     "email":"alexandrebuaizn@gmail.com","telefone":"(27) 3322-9999",
     "cidade_leiloeiro":"Guarapari","uf_leiloeiro":"ES"},
    {"matricula":"007/1984","nome":"ORLANDO LOPES FERNANDES",
     "site":"https://www.leilobras.lel.br/",
     "email":"lopes.fernandes@uol.com.br","telefone":"(27) 3337-5000",
     "cidade_leiloeiro":"Vitória","uf_leiloeiro":"ES"},
    {"matricula":"008/1984","nome":"SÉRGIO DE PAULA PEREIRA",
     "site":"https://www.esleiloes.com.br/",
     "email":"sergiocascao2@gmail.com","telefone":"(27) 99982-3998",
     "cidade_leiloeiro":"Vitória","uf_leiloeiro":"ES"},
    {"matricula":"009/1984","nome":"PATRÍCIA C. ALMEIDA",
     "site":"","email":"pcameida14@gmail.com","telefone":"(27) 3314-1499",
     "cidade_leiloeiro":"Vitória","uf_leiloeiro":"ES"},
    {"matricula":"019/1993","nome":"MARIA AMÉLIA DYNA DE SOUZA",
     "site":"","email":"ameliadyna@yahoo.com.br","telefone":"(27) 3229-2291",
     "cidade_leiloeiro":"Vila Velha","uf_leiloeiro":"ES"},
    {"matricula":"037/1993","nome":"MAURO CESAR ROCHA",
     "site":"http://www.leilofacil.lel.br/",
     "email":"mcesarrocha@terra.com.br","telefone":"(27) 3227-6959",
     "cidade_leiloeiro":"Vitória","uf_leiloeiro":"ES"},
    {"matricula":"039/1993","nome":"SUED PETER BASTOS DYNA",
     "site":"https://www.suedpeterleiloes.com.br/",
     "email":"suedpeter@hotmail.com","telefone":"(27) 99779-8227",
     "cidade_leiloeiro":"Vila Velha","uf_leiloeiro":"ES"},
    {"matricula":"051/2006","nome":"MAURO COLODETE",
     "site":"https://colodeteleiloes.com.br/",
     "email":"sac@colodeteleiloes.com.br","telefone":"(27) 99955-5000",
     "cidade_leiloeiro":"Vitória","uf_leiloeiro":"ES"},
    {"matricula":"052/2007","nome":"HIDIRLENE DUSZEIKO",
     "site":"https://www.hdleiloes.com.br/",
     "email":"hidirlene@hdleiloes.com.br","telefone":"0800 707 9339",
     "cidade_leiloeiro":"Vila Velha","uf_leiloeiro":"ES"},
    {"matricula":"055/2013","nome":"GABRIEL FARDIN PEREIRA",
     "site":"https://www.vixleiloes.com.br/",
     "email":"gabrielleiloes@gmail.com","telefone":"(27) 3315-5148",
     "cidade_leiloeiro":"Vitória","uf_leiloeiro":"ES"},
    {"matricula":"058/2014","nome":"AYRTON DE SOUZA PORTO FILHO",
     "site":"https://www.gestaodeleiloes.com.br/",
     "email":"ayrtonporto@gmail.com","telefone":"(27) 3024-1100",
     "cidade_leiloeiro":"Vitória","uf_leiloeiro":"ES"},
    {"matricula":"061/2015","nome":"PIETRANGELO ROSALÉM",
     "site":"","email":"pietrangelorosalem@gmail.com","telefone":"(27) 99944-7575",
     "cidade_leiloeiro":"Vitória","uf_leiloeiro":"ES"},
    {"matricula":"062/2017","nome":"RENAN NERIS DA SILVA",
     "site":"https://www.renannerisleiloeiro.com.br/",
     "email":"renan290592@gmail.com","telefone":"(27) 99640-6443",
     "cidade_leiloeiro":"Conceição da Barra","uf_leiloeiro":"ES"},
    {"matricula":"063/2018","nome":"FLÁVIA DE OLIVEIRA ROCHA",
     "site":"https://www.leilofacil.lel.br/",
     "email":"fla.rocha@terra.com.br","telefone":"(27) 98128-3929",
     "cidade_leiloeiro":"Vitória","uf_leiloeiro":"ES"},
    {"matricula":"064/2019","nome":"CAROLINE DE SOUSA RIBAS",
     "site":"","email":"desousacarol79@gmail.com","telefone":"(27) 99832-6223",
     "cidade_leiloeiro":"Vila Velha","uf_leiloeiro":"ES"},
    {"matricula":"065/2019","nome":"ALEXSANDER PRETTI DOMINGOS",
     "site":"","email":"alexsanderpretti@hotmail.com","telefone":"(27) 99987-1003",
     "cidade_leiloeiro":"Colatina","uf_leiloeiro":"ES"},
    {"matricula":"066/2019","nome":"BRENNO DE FIGUEIREDO PORTO",
     "site":"https://www.portoleiloes.com.br/",
     "email":"brenno@portoleiloes.com.br","telefone":"(27) 99865-8986",
     "cidade_leiloeiro":"Vila Velha","uf_leiloeiro":"ES"},
    {"matricula":"068/2020","nome":"SANDRA DE FÁTIMA SANTOS",
     "site":"","email":"sandrafsantosleiloeira@gmail.com","telefone":"(32) 98809-4182",
     "cidade_leiloeiro":"Visconde do Rio Branco","uf_leiloeiro":"MG"},
    {"matricula":"069/2020","nome":"RONALD DE FREITAS MOREIRA",
     "site":"","email":"ronaldfmoreira@gmail.com","telefone":"(32) 99199-8124",
     "cidade_leiloeiro":"Visconde do Rio Branco","uf_leiloeiro":"MG"},
    {"matricula":"070/2020","nome":"LUCAS RAFAEL ANTUNES MOREIRA",
     "site":"","email":"lucasleiloeiro@yahoo.com.br","telefone":"(27) 99991-8317",
     "cidade_leiloeiro":"Guarapari","uf_leiloeiro":"ES"},
    {"matricula":"071/2020","nome":"FERNANDO CAETANO MOREIRA FILHO",
     "site":"","email":"fernandoleiloeiro@yahoo.com.br","telefone":"(37) 99862-5659",
     "cidade_leiloeiro":"Contagem","uf_leiloeiro":"MG"},
    {"matricula":"072/2020","nome":"JONAS GABRIEL ANTUNES MOREIRA",
     "site":"","email":"jonasleiloeiro@yahoo.com.br","telefone":"(37) 99862-5727",
     "cidade_leiloeiro":"Para de Minas","uf_leiloeiro":"MG"},
    {"matricula":"073/2020","nome":"GUSTAVO BOLZAN",
     "site":"https://www.gbleiloes.com.br/",
     "email":"gustavo@gbleiloes.com.br","telefone":"(28) 99956-5850",
     "cidade_leiloeiro":"Cachoeiro de Itapemirim","uf_leiloeiro":"ES"},
    {"matricula":"075/2020","nome":"ALEX WILLIAN HOPPE",
     "site":"https://www.hoppeleiloes.com.br/",
     "email":"contato@hoppeleiloes.com.br","telefone":"(47) 3622-5164",
     "cidade_leiloeiro":"Canoinhas","uf_leiloeiro":"SC"},
    {"matricula":"077/2021","nome":"RUDIVAL ALMEIDA GOMES JÚNIOR",
     "site":"","email":"rudival@rjleiloes.com.br","telefone":"(71) 98211-2013",
     "cidade_leiloeiro":"Salvador","uf_leiloeiro":"BA"},
    {"matricula":"078/2021","nome":"JOSÉ SÉRGIO DELLA GIUSTINA",
     "site":"https://www.macedoleiloes.com.br",
     "email":"contato@macedoleiloes.com.br","telefone":"(48) 3030-9600",
     "cidade_leiloeiro":"Florianópolis","uf_leiloeiro":"SC"},
    {"matricula":"081/2021","nome":"MARCUS ALLAIN DE OLIVEIRA BARBOSA",
     "site":"https://www.maleiloesro.com.br",
     "email":"maleiloesro@gmail.com","telefone":"(27) 99750-2672",
     "cidade_leiloeiro":"Vila Velha","uf_leiloeiro":"ES"},
    {"matricula":"083/2022","nome":"PÂMELA DE SOUZA ALVES",
     "site":"","email":"pamelaalvesleiloeira@gmail.com","telefone":"(32) 99834-5630",
     "cidade_leiloeiro":"Guiricema","uf_leiloeiro":"MG"},
    {"matricula":"084/2022","nome":"RUAM CARLOS CHAVES GOTARDO",
     "site":"https://www.serranaleiloes.com.br",
     "email":"contato@serranaleiloes.com.br","telefone":"(27) 98825-4332",
     "cidade_leiloeiro":"Vitória","uf_leiloeiro":"ES"},
    {"matricula":"085/2022","nome":"TIAGO TESSLER BLECHER",
     "site":"https://www.webleiloes.com.br",
     "email":"credenciamento@webleiloes.com.br","telefone":"(11) 3392-3446",
     "cidade_leiloeiro":"Brasília","uf_leiloeiro":"DF"},
    {"matricula":"087/2022","nome":"RENATO SCHLOBACH MOYSES",
     "site":"https://www.majudicial.com.br",
     "email":"renato.moyses@majudicial.com.br","telefone":"(11) 98111-1062",
     "cidade_leiloeiro":"São Paulo","uf_leiloeiro":"SP"},
    {"matricula":"089/2023","nome":"GUSTAVO MORETTO GUIMARÃES DE OLIVEIRA",
     "site":"https://www.gustavomorettoleiloeiro.com.br",
     "email":"contato@gustavomorettoleiloeiro.com.br","telefone":"(19) 3514-3740",
     "cidade_leiloeiro":"Sumaré","uf_leiloeiro":"SP"},
    {"matricula":"090/2023","nome":"GUSTAVO MARTINS ROCHA",
     "site":"https://www.grleiloes.com",
     "email":"grleiloes@grleiloes.com","telefone":"(27) 98144-4535",
     "cidade_leiloeiro":"Vila Velha","uf_leiloeiro":"ES"},
    {"matricula":"091/2023","nome":"CAIO DE CARVALHO BORGES",
     "site":"https://www.cb-leiloeiro.com.br",
     "email":"cb.leiloeiro@gmail.com","telefone":"(028) 9-9987-0458",
     "cidade_leiloeiro":"Cachoeiro de Itapemirim","uf_leiloeiro":"ES"},
    {"matricula":"092/2023","nome":"ALESSANDRO DE ASSIS TEIXEIRA",
     "site":"https://www.alessandroteixeiraleiloes.com.br",
     "email":"contato@alessandroteixeiraleiloes.com.br","telefone":"(35) 99228-1011",
     "cidade_leiloeiro":"Poços de Caldas","uf_leiloeiro":"MG"},
    {"matricula":"093/2023","nome":"DAVI BORGES DE AQUINO",
     "site":"https://www.alfaleiloes.com",
     "email":"inscricoes@alfaleiloes.com","telefone":"(11) 93207-1308",
     "cidade_leiloeiro":"São Paulo","uf_leiloeiro":"SP"},
    {"matricula":"094/2023","nome":"ESTEVÃO STRINI CAMILO",
     "site":"","email":"estevaocamilo@hotmail.com","telefone":"(17) 99137-2470",
     "cidade_leiloeiro":"São José do Rio Preto","uf_leiloeiro":"SP"},
    {"matricula":"095/2023","nome":"MARCO ANTONIO BARBOSA DE OLIVEIRA JUNIOR",
     "site":"https://www.marcoantonioleiloeiro.com.br",
     "email":"juridico@marcoantonioleiloeiro.com.br","telefone":"(31) 98977-8881",
     "cidade_leiloeiro":"Nova Lima","uf_leiloeiro":"MG"},
    {"matricula":"096/2024","nome":"ERICK SOARES TELES",
     "site":"https://www.teza.com.br",
     "email":"teles@teza.com.br","telefone":"(11) 99839-9041",
     "cidade_leiloeiro":"São Paulo","uf_leiloeiro":"SP"},
    {"matricula":"097/2024","nome":"DANIEL MELO CRUZ",
     "site":"https://www.grupolance.com.br",
     "email":"priscilla@grupolance.com.br","telefone":"(13) 99665-0972",
     "cidade_leiloeiro":"Guarujá","uf_leiloeiro":"SP"},
    {"matricula":"098/2024","nome":"JONAS RYMER",
     "site":"","email":"rymerleiloes@gmail.com","telefone":"(21) 98796-9822",
     "cidade_leiloeiro":"Rio de Janeiro","uf_leiloeiro":"RJ"},
    {"matricula":"099/2024","nome":"CARLA KARINE SANTOS AGOSTINHO",
     "site":"","email":"carlaleiloeira@gmail.com","telefone":"(31) 99219-9258",
     "cidade_leiloeiro":"Belo Horizonte","uf_leiloeiro":"MG"},
    {"matricula":"100/2024","nome":"PAULO CESAR AGOSTINHO",
     "site":"https://www.agostinholeiloes.com.br/",
     "email":"agostinho@agostinholeiloes.com.br","telefone":"(31) 98754-5246",
     "cidade_leiloeiro":"Belo Horizonte","uf_leiloeiro":"MG"},
    {"matricula":"101/2024","nome":"VICTOR DE ALMEIDA DOMINGUES CUNHA",
     "site":"","email":"victor@almeidacunha.com","telefone":"(27) 98811-4221",
     "cidade_leiloeiro":"Vila Velha","uf_leiloeiro":"ES"},
    {"matricula":"102/2024","nome":"MARCOS RODRIGO CUSTODIO SOARES",
     "site":"https://www.custodioleiloes.com.br",
     "email":"sac@custodioleiloes.com.br","telefone":"(35) 99958-1439",
     "cidade_leiloeiro":"Franca","uf_leiloeiro":"SP"},
    {"matricula":"103/2024","nome":"DANIEL ELIAS GARCIA",
     "site":"https://danielgarcialeiloes.com.br/",
     "email":"contato@dgleiloes.com.br","telefone":"0800 278 7431",
     "cidade_leiloeiro":"Vitória","uf_leiloeiro":"ES"},
    {"matricula":"104/2024","nome":"THIECO WAYNER MOZART MIGUEL GALVÃO",
     "site":"","email":"thiecowayner@hotmail.com","telefone":"(27) 99829-3299",
     "cidade_leiloeiro":"Vitória","uf_leiloeiro":"ES"},
    {"matricula":"105/2024","nome":"JOAO RENATO LAHAS DI CHIARA",
     "site":"","email":"joaorenatolahas@gmail.com","telefone":"(27) 99691-1999",
     "cidade_leiloeiro":"Vila Velha","uf_leiloeiro":"ES"},
    {"matricula":"106/2024","nome":"MATHEUS WERNECK DE OLIVEIRA SANTOS",
     "site":"","email":"matheuswerneckjm@gmail.com","telefone":"(31) 985679023",
     "cidade_leiloeiro":"São João Monlevade","uf_leiloeiro":"MG"},
    {"matricula":"107/2024","nome":"DORA PLAT",
     "site":"https://www.portalzuk.com.br",
     "email":"dplat@portalzuk.com.br","telefone":"(11) 2184-0909",
     "cidade_leiloeiro":"Taboão da Serra","uf_leiloeiro":"SP"},
    {"matricula":"108/2024","nome":"LILIANE DE NARDE SALLES",
     "site":"https://www.lilianecorretora.com.br",
     "email":"contato@lilianecorretora.com.br","telefone":"(27) 99919-1213",
     "cidade_leiloeiro":"Serra","uf_leiloeiro":"ES"},
    {"matricula":"109/2024","nome":"EDUARDO SCHMITZ",
     "site":"https://www.clicleiloes.com.br",
     "email":"comercial@clicleiloes.com.br","telefone":"(47) 99220-5622",
     "cidade_leiloeiro":"Balneário Camboriú","uf_leiloeiro":"SC"},
    {"matricula":"110/2024","nome":"BRUNO BIRSCHNER LUBE",
     "site":"","email":"lube.bruno@hotmail.com","telefone":"(27) 99994-3007",
     "cidade_leiloeiro":"Cariacica","uf_leiloeiro":"ES"},
    {"matricula":"111/2024","nome":"LUIZ ROBERTO DE OLIVEIRA BRENNEKEN",
     "site":"https://www.lubreleiloes.com.br",
     "email":"luiz.brenneken@gmail.com","telefone":"(71) 99175-9763",
     "cidade_leiloeiro":"Lauro de Freitas","uf_leiloeiro":"BA"},
    # 2025 — do PDF "Leiloeiros ES +" (ainda não na Relação de Regulares mas listados)
    {"matricula":"113/2025","nome":"MANUELA MASAI VILAR VIEIRA DO NASCIMENTO",
     "site":"","email":"empresa.masai@gmail.com","telefone":"(27) 997176911",
     "cidade_leiloeiro":"Vitória","uf_leiloeiro":"ES"},
    {"matricula":"114/2025","nome":"GIOVANA MARQUES COELHO BASTOS",
     "site":"","email":"giovanamcb@hotmail.com","telefone":"(27) 99798-1555",
     "cidade_leiloeiro":"Vitória","uf_leiloeiro":"ES"},
    {"matricula":"116/2025","nome":"SARA CORONA JUNQUEIRA",
     "site":"https://www.leiloescapixaba.com.br",
     "email":"gestao.scj@gmail.com","telefone":"(27) 99786-8593",
     "cidade_leiloeiro":"Baixo Guandu","uf_leiloeiro":"ES"},
    {"matricula":"117/2025","nome":"MARCELO SEPULCRI VALADARES",
     "site":"","email":"marcelosvaladares@gmail.com","telefone":"(27) 99993-3300",
     "cidade_leiloeiro":"Vitória","uf_leiloeiro":"ES"},
    {"matricula":"118/2025","nome":"ELIZABETH DE CARVALHO BORGES",
     "site":"","email":"betsy@vendaemgaragem.com","telefone":"(28) 99258-1511",
     "cidade_leiloeiro":"Cachoeiro de Itapemirim","uf_leiloeiro":"ES"},
    {"matricula":"119/2025","nome":"LUIS OTAVIO MARCOLINO SHINKAWA",
     "site":"","email":"luisotavioshinkawa@hotmail.com","telefone":"(35) 99710-0861",
     "cidade_leiloeiro":"Elói Mendes","uf_leiloeiro":"MG"},
    {"matricula":"120","nome":"COSME MARTINS",
     "site":"","email":"cosmemartins666@gmail.com","telefone":"(27) 99625-5454",
     "cidade_leiloeiro":"Vila Velha","uf_leiloeiro":"ES"},
    {"matricula":"122","nome":"IRANI FLORES",
     "site":"https://www.leilaobrasil.com.br",
     "email":"irani.flores@leilaobrasil.com.br","telefone":"11 3965-0000",
     "cidade_leiloeiro":"São Paulo","uf_leiloeiro":"SP"},
]

for l in LEILOEIROS_JUCEES:
    l["situacao"] = "Regular"
    l["junta"]    = "JUCEES"
    l["fonte"]    = "pdf"

# ── Helpers ────────────────────────────────────────────────────────────────────
def make_id(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:24]

def clean_money(s: str) -> float | None:
    if not s: return None
    s = re.sub(r"[^\d,.]", "", s)
    s = s.replace(".", "").replace(",", ".")
    try: return float(s) if s else None
    except: return None

def parse_date(s: str) -> str | None:
    if not s: return None
    m = re.search(r"(\d{2})[/\-\.](\d{2})[/\-\.](\d{4})", s)
    if m:
        y, mo, d = int(m.group(3)), int(m.group(2)), int(m.group(1))
        try:
            import datetime as dt
            dt.date(y, mo, d)
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            return None
    m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m2:
        return m2.group(0)
    return None

def infer_tipo(titulo: str, desc: str = "") -> str:
    txt = (titulo + " " + desc).lower()
    if any(k in txt for k in ["fazenda","sítio","hectare","rural","chácara","gleba"]): return "rural"
    if any(k in txt for k in ["apart","flat","studio","kitnet"]): return "apartamento"
    if any(k in txt for k in ["casa","sobrado","residência","vila"]): return "casa"
    if any(k in txt for k in ["terreno","lote urbano"]): return "terreno"
    if any(k in txt for k in ["galpão","armazém","depósito","industrial"]): return "galpao"
    if any(k in txt for k in ["sala","conjunto comercial","loja","ponto comercial","prédio"]): return "comercial"
    return "outro"

def infer_tipo_leilao(titulo: str, desc: str = "") -> str:
    txt = (titulo + " " + desc).lower()
    if any(k in txt for k in ["judicial","processo","execução","hasta","praça","tjes","tjmg","trt"]): return "judicial"
    if any(k in txt for k in ["banco","caixa","financiamento","retomada","bancário"]): return "bancario"
    return "extrajudicial"

def derivar_site_do_email(email: str) -> str | None:
    if not email: return None
    email = email.split()[0].strip()
    m = re.search(r"@([a-z0-9\-]+\.[a-z\.]+)", email.lower())
    if not m: return None
    dominio = m.group(1)
    ignorados = {"gmail.com","hotmail.com","yahoo.com","yahoo.com.br",
                 "outlook.com","terra.com.br","uol.com.br","bol.com.br","ig.com.br",
                 "live.com"}
    if dominio in ignorados: return None
    return f"https://www.{dominio}"

RE_PRICE = re.compile(r"R[\$\s]+(\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})?)")
RE_AREA  = re.compile(r"(\d+[\.,]?\d*)\s*m[²2]", re.IGNORECASE)
RE_PROC  = re.compile(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}")
RE_CEP   = re.compile(r"\d{5}-?\d{3}")
RE_UF    = re.compile(r"\b(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MS|MT|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\b")
RE_DATE  = re.compile(r"\d{2}[\/\.\-]\d{2}[\/\.\-]\d{4}")
DOC_KW   = re.compile(
    r"edital|matr[íi]cula|laudo|avalia[cç][aã]o|certid[ãa]o|"
    r"memorial|escritura|penhora|registro|processo",
    re.IGNORECASE
)
PDF_EXT  = re.compile(r"\.pdf(\?[^\"\']*)?$", re.IGNORECASE)

LISTING_PATHS = [
    "/imoveis", "/imoveis/", "/leiloes", "/lotes", "/lotes/",
    "/leilao", "/leiloes/", "/proximos-leiloes", "/proximos_leiloes",
    "/leiloes/imoveis", "/imoveis-leilao", "/em-leilao",
    "/leiloes/imoveis-rurais", "/leiloes/imoveis-urbanos",
    "/catalogo", "/catalogo/", "/ofertas", "/home", "/",
]
LISTING_KW = ["imóv","imovel","imoveis","leilão","leiloes","lote","lotes","oferta","leilao","praça","hasta"]


# ── Scrape do site JUCEES ──────────────────────────────────────────────────────
def fetch_jucees_regulares() -> list[dict]:
    """Tenta buscar lista oficial da JUCEES online. Fallback silencioso se falhar."""
    log(f"Buscando lista oficial JUCEES em {JUCEES_URL} ...")
    try:
        r = requests.get(JUCEES_URL, headers=HEADERS, timeout=30, verify=False)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        dif = f"Falha ao acessar {JUCEES_URL}: {e}"
        log(f"[WARN] {dif}. Usando apenas dados dos PDFs.")
        with _lock:
            _estado["dificuldades"].append({"tipo":"acesso_jucees","msg":dif})
        return []

    regulares = []
    # Site usa cards/blocos por matrícula
    texto = soup.get_text(" ", strip=True)
    # Busca padrões de matrícula + nome
    blocos = re.split(r"matricula\s*:\s*\d+/\d+", texto, flags=re.IGNORECASE)
    for bloco in blocos:
        if "regular" not in bloco.lower():
            continue
        # Extrai nome (primeira linha em maiúsculas)
        m_nome = re.search(r"Nome\s*:\s*([A-ZÁÉÍÓÚÀÂÊÔÃÕÜÇ][^\n\r]{4,80})", bloco, re.IGNORECASE)
        if not m_nome: continue
        nome = m_nome.group(1).strip()

        m_site = re.search(r"Site\s*:\s*(https?://\S+|www\.\S+)", bloco, re.IGNORECASE)
        site = m_site.group(1).strip().rstrip(",;") if m_site else ""
        if site and not site.startswith("http"):
            site = "https://" + site

        m_email = re.search(r"[\w.\-+]+@[\w.\-]+\.\w+", bloco)
        email = m_email.group(0).strip() if m_email else ""

        regulares.append({
            "nome": nome, "site": site, "email": email,
            "junta": "JUCEES", "fonte": "web", "situacao": "Regular",
            "cidade_leiloeiro": "", "uf_leiloeiro": "ES",
        })

    log(f"  JUCEES online: {len(regulares)} registros encontrados")
    return regulares


def merge_leiloeiros(pdf_list: list[dict], web_list: list[dict]) -> list[dict]:
    nomes_vistos = set()
    resultado = list(pdf_list)
    for l in pdf_list:
        nomes_vistos.add(re.sub(r"\s+", " ", l["nome"]).strip().upper())

    for lei in web_list:
        nome_norm = re.sub(r"\s+", " ", lei.get("nome","")).strip().upper()
        if nome_norm in nomes_vistos: continue
        nomes_vistos.add(nome_norm)
        if not lei.get("site") and lei.get("email"):
            lei["site"] = derivar_site_do_email(lei["email"]) or ""
        resultado.append(lei)

    # Preenche site via e-mail para quem não tem site
    for l in resultado:
        if not l.get("site") and l.get("email"):
            l["site"] = derivar_site_do_email(l["email"]) or ""

    com_site = [l for l in resultado if l.get("site")]
    log(f"  Total merged: {len(resultado)} | com site: {len(com_site)}")
    return resultado


# ── Extração de imóveis ────────────────────────────────────────────────────────
def is_imovel(titulo: str, url: str = "") -> bool:
    txt = (titulo + " " + url).lower()
    nao_imovel = ["veículo","veiculo","automóvel","automovel","moto","motocicl",
                  "caminhão","caminhao","trator","máquina","maquina","equipamento",
                  "eletro","celular","notebook","sucata","eletrodom","carro","pick-up",
                  "pickup","camionete","utilitário"]
    imovel_kw  = ["imóvel","imovel","apart","casa","terreno","galpão","sala","loja",
                  "gleba","rural","lote","fazenda","sítio","comercial","prédio",
                  "sobrado","flat","kitnet","chácara","conjunto","edifício","edificio",
                  "quitinete","unidade","ativo imobiliário"]
    if any(k in txt for k in nao_imovel): return False
    if any(k in txt for k in imovel_kw): return True
    return True


def extract_arquivos(soup: BeautifulSoup, page_url: str) -> list[dict]:
    arquivos = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#","javascript","mailto","tel")): continue
        url_abs = urljoin(page_url, href)
        if url_abs in seen: continue
        text = (a.get_text() + " " + href).lower()
        if PDF_EXT.search(href) or DOC_KW.search(text):
            tipo = "edital" if "edital" in text else \
                   "matricula" if ("matric" in text or "matríc" in text) else \
                   "laudo" if "laudo" in text else \
                   "certidao" if "certid" in text else "pdf"
            nome = a.get_text(strip=True)[:80] or tipo.capitalize()
            arquivos.append({"tipo": tipo, "url": url_abs, "nome": nome})
            seen.add(url_abs)
        if len(arquivos) >= 15: break
    # onclick ExibeDoc (padrão tribunais)
    for tag in soup.find_all(onclick=True):
        m = re.search(r"ExibeDoc\(['\"]([^'\"]+)['\"]\)", tag.get("onclick",""))
        if m:
            path = m.group(1)
            url_abs = urljoin(page_url, path)
            if url_abs not in seen:
                tipo = "matricula" if "matricula" in path.lower() else "edital"
                arquivos.append({"tipo": tipo, "url": url_abs, "nome": tipo.capitalize()})
                seen.add(url_abs)
    return arquivos


def extrair_imovel_do_card(card_html: str, card_url: str, lei: dict, base_url: str) -> dict | None:
    soup = BeautifulSoup(card_html, "html.parser")
    texto = soup.get_text(" ", strip=True)

    # Título
    titulo = ""
    for sel in ["h1","h2","h3",".titulo",".title",".lote-titulo","[class*='titulo']",
                "[class*='title']","[class*='lote']","[class*='imovel']"]:
        el = soup.select_one(sel)
        if el:
            titulo = el.get_text(strip=True)[:200]
            if len(titulo) > 5:
                break
    if not titulo:
        # Pega primeira linha substantiva do texto
        linhas = [l.strip() for l in texto.splitlines() if len(l.strip()) > 10]
        titulo = linhas[0][:200] if linhas else texto[:120]

    if not is_imovel(titulo, card_url): return None

    # Preços
    precos = RE_PRICE.findall(texto)
    v_min  = clean_money(precos[0]) if precos else None
    v_aval = clean_money(precos[1]) if len(precos) > 1 else None

    # Área e quartos
    area_m  = RE_AREA.search(texto)
    area    = area_m.group(1).replace(",",".") if area_m else None
    q_m     = re.search(r"(\d)\s*quarto", texto, re.IGNORECASE)
    quartos = int(q_m.group(1)) if q_m else None

    # Datas
    datas = RE_DATE.findall(texto)
    data1 = parse_date(datas[0]) if datas else None
    data2 = parse_date(datas[1]) if len(datas) > 1 else None

    # Localização
    uf_m  = RE_UF.search(texto)
    uf    = uf_m.group() if uf_m else lei.get("uf_leiloeiro","ES")
    cid_m = re.search(rf"([A-ZÀ-Úa-zà-ú]+(?:\s[A-ZÀ-Úa-zà-ú]+){{0,3}})\s*/\s*{uf}", texto)
    cidade = cid_m.group(1).strip() if cid_m else ""

    cep_m  = RE_CEP.search(texto)
    cep    = cep_m.group() if cep_m else ""
    proc_m = RE_PROC.search(texto)
    processo = proc_m.group() if proc_m else ""

    # Imagem
    imgs = soup.find_all("img", src=True)
    img_principal = ""
    for img in imgs:
        src = img.get("src","") or img.get("data-src","") or img.get("data-lazy-src","")
        if not src: continue
        src_abs = urljoin(base_url, src)
        if not any(k in src.lower() for k in ["logo","icon","banner","avatar","sprite","placeholder","blank"]):
            img_principal = src_abs
            break

    # Endereço e descrição
    end_el = soup.select_one("[class*='endere'],[class*='local'],[class*='address'],[itemprop='address']")
    endereco = end_el.get_text(strip=True)[:300] if end_el else ""
    desc_el  = soup.select_one("[class*='descri'],[class*='desc'],[class*='detail'],[class*='detalhe']")
    desc = desc_el.get_text(" ", strip=True)[:500] if desc_el else texto[:300]

    arquivos = extract_arquivos(soup, card_url)

    return {
        "id_externo": make_id(card_url),
        "leiloeiro": lei["nome"],
        "leiloeiro_site": lei.get("site",""),
        "titulo": titulo,
        "tipo_imovel": infer_tipo(titulo, desc),
        "tipo_leilao": infer_tipo_leilao(titulo, desc),
        "estado": uf,
        "cidade": cidade,
        "cep": cep,
        "endereco_completo": endereco,
        "valor_minimo": v_min,
        "valor_avaliacao": v_aval,
        "area_total": area,
        "quartos": quartos,
        "data_primeiro_leilao": data1,
        "data_segundo_leilao": data2,
        "data_encerramento": None,
        "url_original": card_url,
        "imagem_principal": img_principal,
        "numero_processo": processo,
        "arquivos": json.dumps(arquivos, ensure_ascii=False),
        "descricao": desc,
    }


def scrape_site_httpx(lei: dict, max_paginas: int = 8) -> list[dict]:
    base = lei["site"].rstrip("/")
    session = requests.Session()
    session.headers.update(HEADERS)
    imoveis = []
    lote_urls = set()

    listagem_url = None
    for path in [""] + LISTING_PATHS:
        try:
            url = base + path if path else base
            r = session.get(url, timeout=20, allow_redirects=True, verify=False)
            if r.status_code == 200 and any(k in r.text.lower() for k in LISTING_KW):
                listagem_url = url
                break
        except Exception:
            continue

    if not listagem_url:
        return []

    for pag in range(1, max_paginas + 1):
        url_pg = listagem_url if pag == 1 else f"{listagem_url}?pagina={pag}"
        try:
            r = session.get(url_pg, timeout=20, verify=False)
            if r.status_code != 200: break
            soup = BeautifulSoup(r.text, "html.parser")

            novos = 0
            for a in soup.find_all("a", href=True):
                href_abs = urljoin(base, a["href"])
                txt = (a.get_text() + " " + a["href"]).lower()
                if any(k in txt or k in href_abs.lower() for k in
                       ["lote","imovel","imóvel","oferta","arrematacao","imoveis"]):
                    if href_abs not in lote_urls and urlparse(href_abs).netloc:
                        lote_urls.add(href_abs)
                        novos += 1

            if novos == 0 and pag > 1:
                break
            time.sleep(1)
        except Exception:
            break

    for url in list(lote_urls)[:200]:
        try:
            r = session.get(url, timeout=20, verify=False)
            if r.status_code != 200: continue
            im = extrair_imovel_do_card(r.text, url, lei, base)
            if im:
                imoveis.append(im)
            time.sleep(0.8)
        except Exception:
            continue

    return imoveis


def scrape_site_playwright(lei: dict, max_paginas: int = 8) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("[WARN] Playwright não instalado. Pulando site JS-heavy.")
        with _lock:
            _estado["dificuldades"].append({
                "tipo":"playwright_ausente","msg":"Playwright não instalado",
                "leiloeiro":lei["nome"]
            })
        return []

    base = lei["site"].rstrip("/")
    imoveis = []
    lote_urls = set()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                ignore_https_errors=True,
            )
            page = context.new_page()

            listagem_url = None
            for path in [""] + LISTING_PATHS:
                try:
                    url = base + path if path else base
                    page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
                    if any(k in page.content().lower() for k in LISTING_KW):
                        listagem_url = url
                        break
                except Exception:
                    continue

            if not listagem_url:
                browser.close()
                return []

            for pag in range(1, max_paginas + 1):
                url_pg = listagem_url if pag == 1 else f"{listagem_url}?pagina={pag}"
                try:
                    page.goto(url_pg, timeout=30000, wait_until="networkidle")
                    page.wait_for_timeout(2000)
                    html = page.content()
                    soup = BeautifulSoup(html, "html.parser")

                    novos = 0
                    for a in soup.find_all("a", href=True):
                        href_abs = urljoin(base, a["href"])
                        txt = (a.get_text() + " " + a["href"]).lower()
                        if any(k in txt or k in href_abs.lower() for k in
                               ["lote","imovel","imóvel","oferta","imoveis"]):
                            if href_abs not in lote_urls and urlparse(href_abs).netloc:
                                lote_urls.add(href_abs)
                                novos += 1

                    if novos == 0 and pag > 1:
                        break
                    time.sleep(2)
                except Exception:
                    break

            for url in list(lote_urls)[:200]:
                try:
                    page.goto(url, timeout=30000, wait_until="networkidle")
                    page.wait_for_timeout(1500)
                    im = extrair_imovel_do_card(page.content(), url, lei, base)
                    if im:
                        imoveis.append(im)
                    time.sleep(1)
                except Exception:
                    continue

            browser.close()
    except Exception as e:
        log(f"  [Playwright ERR] {e}")
        with _lock:
            _estado["dificuldades"].append({
                "tipo":"playwright_erro","msg":str(e),"leiloeiro":lei["nome"]
            })

    return imoveis


def is_js_heavy(html: str) -> bool:
    markers = ["__next_data__","__nuxt__","react-root","vue-app","ng-app",
               "window.__initial_state__","data-reactroot"]
    html_lower = html.lower()
    if any(m in html_lower for m in markers): return True
    soup = BeautifulSoup(html, "html.parser")
    texto = soup.get_text()
    return len(texto.strip()) < 300


def scrape_leiloeiro(lei: dict, max_paginas: int = 8) -> tuple[list[dict], str]:
    if not lei.get("site"):
        return [], "sem_site"

    site = lei["site"]
    log(f"  → Scraping: {lei['nome']} | {site}")

    try:
        r = requests.get(site, timeout=15, headers=HEADERS, verify=False, allow_redirects=True)
        html = r.text
        if r.status_code in (404, 410, 503):
            dif = f"Site retornou HTTP {r.status_code}"
            log(f"    [WARN] {dif}")
            with _lock:
                _estado["dificuldades"].append({
                    "tipo":"http_error","msg":dif,
                    "leiloeiro":lei["nome"],"site":site
                })
            return [], "offline"
        if r.status_code == 403:
            dif = f"Site bloqueado (403/Cloudflare): {site}"
            log(f"    [WARN] {dif}")
            with _lock:
                _estado["dificuldades"].append({
                    "tipo":"cloudflare_ou_403","msg":dif,
                    "leiloeiro":lei["nome"],"site":site
                })
            # Tenta Playwright como fallback
            imoveis = scrape_site_playwright(lei, max_paginas)
            return imoveis, "ok" if imoveis else "sem_leilao"
    except requests.exceptions.SSLError as e:
        dif = f"Erro SSL em {site}: {e}"
        log(f"    [WARN] {dif}")
        with _lock:
            _estado["dificuldades"].append({"tipo":"ssl_error","msg":dif,"leiloeiro":lei["nome"]})
        try:
            r = requests.get(site, timeout=15, headers=HEADERS, verify=False, allow_redirects=True)
            html = r.text
        except Exception as e2:
            dif2 = f"HTTP falhou mesmo sem SSL: {e2}"
            with _lock:
                _estado["dificuldades"].append({"tipo":"http_fail","msg":dif2,"leiloeiro":lei["nome"]})
            imoveis = scrape_site_playwright(lei, max_paginas)
            return imoveis, "ok" if imoveis else "sem_leilao"
    except requests.exceptions.ConnectionError as e:
        dif = f"Conexão recusada / site offline: {site}"
        log(f"    [WARN] {dif}")
        with _lock:
            _estado["dificuldades"].append({"tipo":"site_offline","msg":dif,"leiloeiro":lei["nome"]})
        return [], "offline"
    except Exception as e:
        dif = f"Erro HTTP inesperado: {e}"
        log(f"    [WARN] {dif}. Tentando Playwright...")
        with _lock:
            _estado["dificuldades"].append({"tipo":"http_fail","msg":dif,"leiloeiro":lei["nome"]})
        imoveis = scrape_site_playwright(lei, max_paginas)
        return imoveis, "ok" if imoveis else "sem_leilao"

    if is_js_heavy(html):
        log(f"    JS-heavy detectado → Playwright")
        imoveis = scrape_site_playwright(lei, max_paginas)
    else:
        imoveis = scrape_site_httpx(lei, max_paginas)
        if not imoveis:
            log(f"    HTTP sem resultados → Playwright")
            imoveis = scrape_site_playwright(lei, max_paginas)

    if not imoveis:
        with _lock:
            _estado["dificuldades"].append({
                "tipo":"sem_imoveis","msg":f"Nenhum imóvel encontrado em {site}",
                "leiloeiro":lei["nome"],"site":site
            })

    return imoveis, "ok" if imoveis else "sem_leilao"


# ── Progresso e relatórios ─────────────────────────────────────────────────────
def salvar_progresso():
    with _lock:
        data = {
            "atualizado": datetime.now().isoformat(),
            "total_imoveis": len(_estado["imoveis"]),
            "por_leiloeiro": _estado["por_leiloeiro"],
            "sites_ok": _estado["sites_ok"],
            "sites_err": _estado["sites_err"],
            "sites_sem_leilao": _estado["sites_sem_leilao"],
            "erros": _estado["erros"][-10:],
        }
    try:
        PROGRESS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def relatorio_5min():
    with _lock:
        por_lei = dict(_estado["por_leiloeiro"])
        total   = len(_estado["imoveis"])
        atual   = _estado["leiloeiro_atual"]

    log(f"\n{'='*60}")
    log(f"RELATÓRIO PARCIAL | Total: {total} imóveis | Agora: {atual}")
    for nome, cnt in sorted(por_lei.items(), key=lambda x: -x[1]):
        log(f"  {nome[:40]:<40} {cnt:>4} imóveis")
    log(f"{'='*60}\n")


def thread_relatorio(stop_evt: threading.Event):
    while not stop_evt.wait(300):
        relatorio_5min()


# ── CSVs ───────────────────────────────────────────────────────────────────────
def salvar_csv_leiloeiros(leiloeiros: list[dict]):
    CSV_DIR.mkdir(exist_ok=True)
    path = CSV_DIR / f"leiloeiros_jucees_{TODAY}.csv"
    campos = ["nome","matricula","site","email","telefone",
              "cidade_leiloeiro","uf_leiloeiro","situacao","junta"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(leiloeiros)
    log(f"[CSV] Leiloeiros: {path} ({len(leiloeiros)} registros)")
    return path


def salvar_csv_imoveis(imoveis: list[dict]):
    CSV_DIR.mkdir(exist_ok=True)
    path = CSV_DIR / f"imoveis_jucees_{TODAY}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES_IMOVEIS, extrasaction="ignore")
        w.writeheader()
        w.writerows(imoveis)
    log(f"[CSV] Imóveis: {path} ({len(imoveis)} registros)")
    return path


# ── SQLite ──────────────────────────────────────────────────────────────────────
def importar_sqlite(imoveis: list[dict]):
    log(f"\n[SQLite] Importando {len(imoveis)} imóveis...")
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS imoveis (
            id TEXT PRIMARY KEY,
            leiloeiro TEXT, junta TEXT, site TEXT,
            titulo TEXT, descricao TEXT, endereco TEXT, cidade TEXT, uf TEXT,
            lance_inicial REAL, avaliacao REAL, data_leilao TEXT,
            url TEXT, tipo TEXT, imagem TEXT, importado_em TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_uf ON imoveis(uf)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_leiloeiro ON imoveis(leiloeiro)")
    conn.commit()

    ins = dup = 0
    agora = datetime.now().isoformat(timespec="seconds")
    for r in imoveis:
        def _d(v):
            try: return float(Decimal(str(v).replace(",","."))) if v else None
            except: return None
        try:
            conn.execute(
                "INSERT INTO imoveis VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    r.get("id_externo",""), r.get("leiloeiro",""), "JUCEES",
                    r.get("leiloeiro_site",""),
                    r.get("titulo","")[:500], r.get("descricao","")[:300],
                    r.get("endereco_completo","")[:200],
                    r.get("cidade",""), r.get("estado","ES"),
                    _d(r.get("valor_minimo")), _d(r.get("valor_avaliacao")),
                    r.get("data_primeiro_leilao",""),
                    r.get("url_original",""), r.get("tipo_imovel",""),
                    r.get("imagem_principal",""), agora,
                )
            )
            ins += 1
        except sqlite3.IntegrityError:
            dup += 1
        except Exception as e:
            log(f"  [SQLite ERR] {e}")

    conn.commit()
    conn.close()
    log(f"  SQLite: {ins} inseridos, {dup} já existiam")
    return ins


# ── PostgreSQL ──────────────────────────────────────────────────────────────────
def psql(sql: str, timeout: int = 30) -> str:
    import subprocess
    proc = subprocess.run(
        ["docker", "exec", "leilao_postgres",
         "psql", "-U", "leilao", "-d", "leilao_db",
         "--no-align", "--tuples-only", "-c", sql],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout
    )
    return proc.stdout


def importar_postgres(imoveis: list[dict]):
    import subprocess
    log(f"\n[PostgreSQL] Importando {len(imoveis)} imóveis...")

    psql("""
        INSERT INTO fontes (nome, url_base, ativo, criado_em)
        VALUES ('JUCEES','https://leiloeiros.jucees.es.gov.br/',true,NOW())
        ON CONFLICT (nome) DO NOTHING;
    """)
    fonte_id_raw = psql("SELECT id FROM fontes WHERE nome='JUCEES' LIMIT 1;").strip()
    if not fonte_id_raw.isdigit():
        log(f"[ERRO] Não foi possível obter fonte_id JUCEES: {repr(fonte_id_raw)}")
        with _lock:
            _estado["dificuldades"].append({
                "tipo":"postgres_fonte","msg":f"fonte_id inválido: {repr(fonte_id_raw)}"
            })
        return 0, 0

    FONTE_ID = int(fonte_id_raw)
    log(f"  fonte_id JUCEES = {FONTE_ID}")

    TIPOS_IMOVEL_VALIDOS = {"APARTAMENTO","CASA","TERRENO","COMERCIAL","RURAL","GALPAO","SALA","VAGA","OUTRO"}
    TIPOS_LEILAO_VALIDOS = {"JUDICIAL","EXTRAJUDICIAL","BANCARIO"}

    ins_pg = upd_pg = err_pg = 0

    for i in range(0, len(imoveis), 50):
        batch = imoveis[i:i+50]
        values = []

        for r in batch:
            def esc(v, max_len=None):
                s = str(v or "").replace("'", "''").replace("\x00", "")
                if max_len: s = s[:max_len]
                return s

            tipo_i = r.get("tipo_imovel","outro").upper()
            if tipo_i not in TIPOS_IMOVEL_VALIDOS: tipo_i = "OUTRO"
            tipo_l = r.get("tipo_leilao","extrajudicial").upper()
            if tipo_l not in TIPOS_LEILAO_VALIDOS: tipo_l = "EXTRAJUDICIAL"

            def _d(v):
                try: return float(Decimal(str(v).replace(",","."))) if v else None
                except: return None

            vmin    = _d(r.get("valor_minimo"))
            vaval   = _d(r.get("valor_avaliacao"))
            area    = _d(r.get("area_total"))
            quartos = r.get("quartos","")
            try: quartos = int(quartos) if quartos else None
            except: quartos = None

            d1 = r.get("data_primeiro_leilao","")
            d2 = r.get("data_segundo_leilao","")

            values.append(f"""(
                {FONTE_ID},
                '{esc(r.get("id_externo",""),200)}',
                '{esc(r.get("titulo",""),500)}',
                '{esc(r.get("descricao",""),500)}',
                '{esc(r.get("url_original",""),1000)}',
                '{tipo_i}', '{tipo_l}',
                'ABERTO', 'IMOVEL',
                '{esc(r.get("cidade",""),200)}',
                '{esc(r.get("estado","ES"),2)}',
                '{esc(r.get("cep",""),10)}',
                '{esc(r.get("endereco_completo",""),500)}',
                {vmin if vmin is not None else 'NULL'},
                {vaval if vaval is not None else 'NULL'},
                {area if area is not None else 'NULL'},
                {quartos if quartos is not None else 'NULL'},
                {f"'{d1}'" if d1 else 'NULL'},
                {f"'{d2}'" if d2 else 'NULL'},
                '{esc(r.get("imagem_principal",""),1000)}',
                '{esc(r.get("arquivos","[]"),4000)}',
                '{esc(r.get("numero_processo",""),100)}',
                '{esc(r.get("leiloeiro",""),300)}',
                true, false, false,
                NOW(), NOW()
            )""")

        if not values:
            continue

        sql = f"""
        INSERT INTO imoveis (
            fonte_id, id_externo, titulo, descricao, url_original,
            tipo_imovel, tipo_leilao, status, categoria,
            cidade, estado, cep, endereco_completo,
            valor_minimo, valor_avaliacao, area_total, quartos,
            data_primeiro_leilao, data_segundo_leilao,
            imagem_principal, arquivos, numero_processo,
            leiloeiro, ativo, classificado, geocodificado,
            criado_em, atualizado_em
        ) VALUES {', '.join(values)}
        ON CONFLICT (fonte_id, id_externo) DO UPDATE SET
            titulo = EXCLUDED.titulo,
            valor_minimo = EXCLUDED.valor_minimo,
            data_primeiro_leilao = EXCLUDED.data_primeiro_leilao,
            imagem_principal = EXCLUDED.imagem_principal,
            arquivos = EXCLUDED.arquivos,
            atualizado_em = NOW();
        """

        try:
            proc = subprocess.run(
                ["docker", "exec", "leilao_postgres",
                 "psql", "-U", "leilao", "-d", "leilao_db", "-c", sql],
                capture_output=True, text=True, encoding="utf-8", timeout=60
            )
            out = proc.stdout + proc.stderr
            if "INSERT" in out:
                m = re.search(r"INSERT \d+ (\d+)", out)
                n = int(m.group(1)) if m else len(batch)
                ins_pg += n
            elif "UPDATE" in out or "conflict" in out.lower():
                upd_pg += len(batch)
            elif proc.returncode != 0:
                err_pg += len(batch)
                log(f"  [ERR lote {i}] {out[:200]}")
                with _lock:
                    _estado["dificuldades"].append({
                        "tipo":"postgres_insert","msg":out[:200]
                    })
        except Exception as e:
            err_pg += len(batch)
            log(f"  [ERR lote {i}] {e}")
            with _lock:
                _estado["dificuldades"].append({"tipo":"postgres_insert","msg":str(e)})

        if (i // 50) % 2 == 0:
            log(f"  Lote {i//50+1}: {ins_pg} inseridos, {upd_pg} atualizados, {err_pg} erros")

    log(f"  PostgreSQL: {ins_pg} inseridos, {upd_pg} atualizados, {err_pg} erros")
    return ins_pg, upd_pg


# ── Relatório de dificuldades → markdown ───────────────────────────────────────
def gerar_relatorio_dificuldades(
    leiloeiros: list[dict],
    todos_imoveis: list[dict],
    por_leiloeiro: dict,
) -> str:
    with _lock:
        difs = list(_estado["dificuldades"])
        sites_ok   = _estado["sites_ok"]
        sites_err  = _estado["sites_err"]
        sites_sl   = _estado["sites_sem_leilao"]

    agora = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Agrupa por tipo
    por_tipo: dict[str, list] = {}
    for d in difs:
        tp = d.get("tipo","outro")
        por_tipo.setdefault(tp, []).append(d)

    linhas = [
        "",
        "---",
        "",
        f"## 37. Scraping JUCEES (ES) — Relatório de dificuldades ({agora})",
        "",
        "### 37.1. Resumo da execução",
        "",
        f"| Métrica | Valor |",
        f"|---|---|",
        f"| Leiloeiros REGULAR encontrados | {len(leiloeiros)} |",
        f"| Leiloeiros com site | {len([l for l in leiloeiros if l.get('site')])} |",
        f"| Sites com imóveis | {sites_ok} |",
        f"| Sites sem leilão ativo | {sites_sl} |",
        f"| Sites com erro / offline | {sites_err} |",
        f"| Total de imóveis coletados | {len(todos_imoveis)} |",
        f"| CSV gerado | `csv/leiloeiros_jucees_{TODAY}.csv` |",
        f"| CSV imóveis | `csv/imoveis_jucees_{TODAY}.csv` |",
        "",
        "### 37.2. Imóveis por leiloeiro",
        "",
        "| Leiloeiro | Site | Imóveis |",
        "|---|---|---|",
    ]

    for lei in sorted(leiloeiros, key=lambda x: por_leiloeiro.get(x["nome"],0), reverse=True):
        cnt  = por_leiloeiro.get(lei["nome"], 0)
        site = lei.get("site","—") or "—"
        linhas.append(f"| {lei['nome']} | {site} | {cnt} |")

    linhas += [
        "",
        "### 37.3. Dificuldades encontradas",
        "",
    ]

    desc_tipos = {
        "acesso_jucees":   "Falha ao acessar o site da JUCEES",
        "site_offline":    "Site do leiloeiro offline / DNS inválido",
        "http_error":      "Erro HTTP (404, 503, etc.)",
        "cloudflare_ou_403": "Bloqueio Cloudflare / WAF (403)",
        "ssl_error":       "Erro de certificado SSL",
        "http_fail":       "Falha na requisição HTTP",
        "playwright_ausente": "Playwright não instalado",
        "playwright_erro": "Erro interno do Playwright",
        "sem_imoveis":     "Site acessado mas sem imóveis encontrados",
        "postgres_fonte":  "Erro ao obter fonte_id no PostgreSQL",
        "postgres_insert": "Erro ao inserir no PostgreSQL",
    }

    if not difs:
        linhas.append("Nenhuma dificuldade registrada.")
    else:
        for tp, items in sorted(por_tipo.items()):
            titulo_dif = desc_tipos.get(tp, tp)
            linhas.append(f"#### {titulo_dif} ({len(items)} ocorrência{'s' if len(items)>1 else ''})")
            linhas.append("")
            for d in items[:5]:
                lei_str = f" — {d['leiloeiro']}" if d.get("leiloeiro") else ""
                linhas.append(f"- `{d.get('site', d.get('msg','')[:80])}`{lei_str}")
            if len(items) > 5:
                linhas.append(f"- *(+{len(items)-5} ocorrências omitidas)*")
            linhas.append("")

    linhas += [
        "### 37.4. Sugestões de correção",
        "",
        "| Problema | Causa | Correção sugerida |",
        "|---|---|---|",
        "| Sites sem leilão ativo | Leiloeiro sem eventos abertos no momento | Reagendar scraping; adicionar monitoramento periódico |",
        "| Site offline / DNS inválido | Site encerrado ou URL desatualizada | Verificar URL manualmente; contatar leiloeiro; atualizar PDF da JUCEES |",
        "| Cloudflare / WAF (403) | Proteção anti-bot ativa | Usar FlareSolverr (Docker :8191) — ver **seção 14** deste guia |",
        "| Erro SSL | Certificado inválido ou expirado | Já contornado com `verify=False`; avisar o leiloeiro |",
        "| JS-heavy sem imóveis (Playwright) | SPA carrega dados via API interna não interceptada | Inspecionar DevTools → XHR; criar extrator dedicado com `page.on('response')` |",
        "| Leiloeiros sem site | Campo Site em branco na JUCEES | Derivar site do e-mail (domínio não-genérico); buscar manualmente |",
        "| Leiloeiros 2025 não na Relação Regulares | PDF pode estar desatualizado | Fazer scraping direto do site https://leiloeiros.jucees.es.gov.br/ com filtro `regular` |",
        "| Preços não extraídos | HTML sem padrão `R$` ou preço em atributo JS | Ampliar janela de regex; interceptar JSON da API interna |",
        "| Imagens com URL relativa quebrada | `urljoin` não resolve alguns CDNs | Adicionar `data-src` e `data-lazy-src` ao extrator de imagens |",
        "| PostgreSQL: fonte_id não encontrado | Container não rodando ou tabela `fontes` ausente | Verificar `docker ps`; rodar migration antes de importar |",
        "",
        "### 37.5. Próximos passos",
        "",
        "1. Para sites com Cloudflare: instalar FlareSolverr e adaptar `scrape_site_playwright` conforme seção 14.",
        "2. Rodar `python run.py classificar --limite 5000` no container para classificar os imóveis importados.",
        "3. Rodar `python run.py deduplicar` para remover duplicatas.",
        "4. Rodar `python run.py baixar-docs --limite 200` para baixar PDFs (editais/matrículas).",
        "5. Agendar re-scraping semanal com `CronCreate` para manter base atualizada.",
        "",
    ]

    return "\n".join(linhas)


# ── Main ────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sem-banco",   action="store_true", help="Não importa para bancos")
    ap.add_argument("--max-sites",   type=int, default=999)
    ap.add_argument("--max-paginas", type=int, default=8)
    ap.add_argument("--reset",       action="store_true")
    args = ap.parse_args()

    log("="*60)
    log(f"JUCEES Scraper iniciado — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"Fonte: PDFs JUCEES (Regular) + site {JUCEES_URL}")
    log("="*60)

    # 1. Lista base dos PDFs
    pdf_list = list(LEILOEIROS_JUCEES)
    log(f"Leiloeiros do PDF: {len(pdf_list)}")

    # 2. Tenta atualizar com site JUCEES
    web_list = fetch_jucees_regulares()

    # 3. Merge
    todos = merge_leiloeiros(pdf_list, web_list)

    # Deduplica por site
    sites_vistos: set[str] = set()
    leiloeiros_unicos = []
    sem_site = []
    for l in todos:
        site = (l.get("site") or "").rstrip("/")
        if not site:
            sem_site.append(l)
            continue
        if site not in sites_vistos:
            sites_vistos.add(site)
            leiloeiros_unicos.append(l)

    log(f"\nTotal Regular: {len(todos)}")
    log(f"  Com site (únicos): {len(leiloeiros_unicos)}")
    log(f"  Sem site: {len(sem_site)}")

    # Salva CSV de leiloeiros (todos)
    salvar_csv_leiloeiros(todos)

    # 4. Thread de relatório a cada 5 min
    stop_evt   = threading.Event()
    t_report   = threading.Thread(target=thread_relatorio, args=(stop_evt,), daemon=True)
    t_report.start()

    todos_imoveis = []
    limitar = min(args.max_sites, len(leiloeiros_unicos))

    for idx, lei in enumerate(leiloeiros_unicos[:limitar], 1):
        with _lock:
            _estado["leiloeiro_atual"] = lei["nome"]

        log(f"\n[{idx}/{limitar}] {lei['nome']} | {lei.get('site','')}")

        try:
            imoveis, status = scrape_leiloeiro(lei, args.max_paginas)
        except Exception as e:
            imoveis, status = [], "erro"
            with _lock:
                _estado["erros"].append((lei.get("site",""), str(e)))
                _estado["dificuldades"].append({
                    "tipo":"excecao_geral","msg":str(e),"leiloeiro":lei["nome"]
                })
            log(f"  [ERRO] {e}")

        with _lock:
            if status == "ok":
                _estado["sites_ok"] += 1
            elif status == "erro":
                _estado["sites_err"] += 1
            else:
                _estado["sites_sem_leilao"] += 1
            _estado["imoveis"].extend(imoveis)
            _estado["por_leiloeiro"][lei["nome"]] = len(imoveis)

        todos_imoveis.extend(imoveis)
        log(f"  Status: {status} | {len(imoveis)} imóveis | Total: {len(todos_imoveis)}")

        salvar_progresso()
        time.sleep(2)

    stop_evt.set()

    # 5. Relatório final
    log(f"\n{'='*60}")
    log(f"SCRAPING CONCLUÍDO: {len(todos_imoveis)} imóveis de {len(leiloeiros_unicos)} sites")
    relatorio_5min()

    # 6. CSVs
    if todos_imoveis:
        salvar_csv_imoveis(todos_imoveis)

    # 7. Bancos
    if not args.sem_banco:
        if todos_imoveis:
            importar_sqlite(todos_imoveis)
            importar_postgres(todos_imoveis)
        else:
            log("[WARN] Nenhum imóvel coletado — banco não atualizado.")

    # 8. Relatório de dificuldades → captura_dados_leiloes_v2.md
    with _lock:
        por_lei_final = dict(_estado["por_leiloeiro"])

    md_relatorio = gerar_relatorio_dificuldades(todos, todos_imoveis, por_lei_final)

    md_file = BASE / "captura_dados_leiloes_v2.md"
    try:
        conteudo_atual = md_file.read_text(encoding="utf-8")
        # Evita duplicar se já existe seção 37
        if "## 37. Scraping JUCEES" in conteudo_atual:
            # Remove seção anterior e reescreve
            idx37 = conteudo_atual.find("\n## 37. Scraping JUCEES")
            if idx37 >= 0:
                conteudo_atual = conteudo_atual[:idx37]
        md_file.write_text(conteudo_atual + "\n" + md_relatorio, encoding="utf-8")
        log(f"[MD] Relatório de dificuldades adicionado em {md_file.name}")
    except Exception as e:
        log(f"[ERRO] Falha ao atualizar {md_file}: {e}")

    # Sumário
    log("\n[CONCLUSÃO]")
    log(f"  Total leiloeiros Regular: {len(todos)}")
    log(f"  Com site (processados): {limitar}")
    log(f"  Sites com imóveis: {_estado['sites_ok']}")
    log(f"  Sites sem leilão ativo: {_estado['sites_sem_leilao']}")
    log(f"  Sites com erro/offline: {_estado['sites_err']}")
    log(f"  Total de imóveis: {len(todos_imoveis)}")
    log(f"  CSV leiloeiros: csv/leiloeiros_jucees_{TODAY}.csv")
    if todos_imoveis:
        log(f"  CSV imóveis: csv/imoveis_jucees_{TODAY}.csv")


if __name__ == "__main__":
    main()
