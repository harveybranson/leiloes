"""
Prepara csv/leiloeiros_todos.csv a partir do arquivo de leiloeiros.
Procura automaticamente em locais comuns (Downloads, Desktop, projeto).
Também aceita o caminho como argumento.

Uso:
    python preparar_csv_leiloeiros.py
    python preparar_csv_leiloeiros.py "C:/Users/Meu/Downloads/meu_arquivo.csv"
"""
import sys, io, csv, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from pathlib import Path

BASE_DIR = Path(r"c:\Users\arthur\OneDrive\Documentos\Cursor\leiloes")
CSV_DIR  = BASE_DIR / "csv"
OUT      = CSV_DIR / "leiloeiros_todos.csv"

# Locais para buscar o arquivo fonte
def candidate_paths(arg: str | None) -> list[Path]:
    user_home = Path.home()
    candidates = []
    if arg:
        candidates.append(Path(arg))
    # Nomes possíveis do arquivo
    names = [
        "leiloeiro replit retirados powerbi retirado de leiloeiro e tradutores.csv",
        "leiloeiros_todos.csv",
        "leiloeiros.csv",
        "leiloeiros_all.csv",
        "leiloeiros_completo.csv",
    ]
    search_dirs = [
        user_home / "Downloads",
        user_home / "Desktop",
        user_home / "Documents",
        user_home / "OneDrive" / "Downloads",
        user_home / "OneDrive" / "Desktop",
        user_home / "OneDrive" / "Documentos",
        BASE_DIR,
        CSV_DIR,
        Path("."),
    ]
    for d in search_dirs:
        if d.exists():
            for n in names:
                p = d / n
                if p.exists():
                    candidates.append(p)
            # Busca pattern amplo
            try:
                for p in d.glob("*leiloeiro*.csv"):
                    candidates.append(p)
                for p in d.glob("*JUCEMG*.csv"):
                    candidates.append(p)
                for p in d.glob("*JUCESP*.csv"):
                    candidates.append(p)
            except: pass
    return candidates

def detect_encoding(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            with open(path, encoding=enc) as f:
                f.read(4096)
            return enc
        except: pass
    return "latin-1"

def has_leiloeiro_data(path: Path) -> bool:
    """Verifica se o arquivo tem o formato esperado."""
    enc = detect_encoding(path)
    try:
        with open(path, encoding=enc) as f:
            header = f.readline().lower()
        return "situaç" in header or "situac" in header or "regular" in header.lower()
    except: return False

def process_csv(src: Path) -> int:
    """Lê o CSV fonte, filtra, e escreve em csv/leiloeiros_todos.csv."""
    enc = detect_encoding(src)
    print(f"Lendo: {src}  (encoding={enc})")

    rows = []
    with open(src, encoding=enc, errors="replace") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        print("ERRO: arquivo vazio ou sem dados.")
        return 0

    headers = list(rows[0].keys())
    print(f"  Colunas: {headers[:6]}")
    print(f"  Total de linhas: {len(rows)}")

    CSV_DIR.mkdir(exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"  Salvo em: {OUT} ({len(rows)} linhas)")
    return len(rows)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    candidates = candidate_paths(arg)

    # Remove duplicatas
    seen = set()
    unique = []
    for p in candidates:
        rp = str(p.resolve())
        if rp not in seen and p.exists():
            seen.add(rp)
            unique.append(p)

    if not unique:
        print("\n" + "="*60)
        print("  ARQUIVO NÃO ENCONTRADO")
        print("="*60)
        print("\nSalve o CSV de leiloeiros em um dos locais abaixo e")
        print("execute este script novamente:\n")
        print(f"  1. {OUT}  (direto)")
        print(f"  2. {Path.home()/'Downloads'/'leiloeiros_todos.csv'}")
        print(f"\nOu execute:")
        print(f"  python preparar_csv_leiloeiros.py 'caminho/para/arquivo.csv'")
        return

    # Tenta cada candidato
    for path in unique:
        print(f"\nVerificando: {path}")
        if not has_leiloeiro_data(path):
            print("  Não parece ter dados de leiloeiros. Pulando.")
            continue
        n = process_csv(path)
        if n > 0:
            print(f"\n✓ CSV pronto! {n} leiloeiros em {OUT}")
            print(f"\nAgora execute:")
            print(f"  python scraper_leiloeiros_direto.py")
            return

    print("\nNenhum arquivo válido encontrado.")
    print(f"Salve o CSV em: {OUT}")


if __name__ == "__main__":
    main()
