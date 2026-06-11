# -*- coding: utf-8 -*-
"""
enrich_local.py — enriquecimento OFFLINE (sem rede) a partir do texto já no banco.

Referência: captura_dados_leiloes_master.md (Parte VII normalização + Parte X DoD). Ataca
as reprovações do gate de cobertura usando o que já está armazenado:

  - uf: deduz a sigla de titulo/endereco/descricao/cidade via scraper_commons.inferir_uf
        (padrão Cidade/UF + nome de município único no IBGE). Nunca grava UF inválida.
  - lance_inicial: parseia "R$ ..." de titulo/descricao quando o campo está nulo e o valor
        existe no texto (recuperação parcial; o grosso vem do re-scrape de detalhe).

É idempotente: só preenche campos vazios. Mostra cobertura antes/depois.

Uso:
  python enrich_local.py                 # aplica no banco padrão
  python enrich_local.py --dry-run       # só simula e relata, não grava
  python enrich_local.py --db outro.db
"""
import argparse
import re
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import scraper_commons as sc

DB_PADRAO = "imoveis_leiloeiros.db"
_PRICE_RE = re.compile(r"R\$\s*([\d.]{1,3}(?:\.\d{3})*,\d{2})")


def _parse_brl(txt):
    m = _PRICE_RE.search(txt or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _vazio(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def _pct(con, expr):
    tot = con.execute("SELECT COUNT(*) FROM imoveis").fetchone()[0]
    ok = con.execute(f"SELECT COUNT(*) FROM imoveis WHERE {expr}").fetchone()[0]
    return 100.0 * ok / tot if tot else 0.0


def enriquecer(db_path, dry_run=False):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    uf_antes = _pct(con, "uf IS NOT NULL AND TRIM(uf) <> ''")
    lance_antes = _pct(con, "lance_inicial IS NOT NULL")

    upd_uf, upd_lance = [], []
    for r in con.execute("SELECT id, titulo, descricao, endereco, cidade, uf, lance_inicial "
                          "FROM imoveis"):
        if _vazio(r["uf"]):
            uf = sc.inferir_uf(r["titulo"], r["endereco"], r["descricao"], r["cidade"])
            if uf:
                upd_uf.append((uf, r["id"]))
        if r["lance_inicial"] is None:
            val = _parse_brl(r["titulo"]) or _parse_brl(r["descricao"])
            if val and val > 500:
                upd_lance.append((val, r["id"]))

    print(f"  uf:            {uf_antes:5.1f}%  ->  +{len(upd_uf)} preenchidos")
    print(f"  lance_inicial: {lance_antes:5.1f}%  ->  +{len(upd_lance)} preenchidos "
          f"(texto; resto exige re-scrape de detalhe)")

    if dry_run:
        print("  [dry-run] nada gravado.")
        con.close()
        return

    con.executemany("UPDATE imoveis SET uf = ? WHERE id = ?", upd_uf)
    con.executemany("UPDATE imoveis SET lance_inicial = ? WHERE id = ?", upd_lance)
    con.commit()

    uf_dep = _pct(con, "uf IS NOT NULL AND TRIM(uf) <> ''")
    lance_dep = _pct(con, "lance_inicial IS NOT NULL")
    print(f"  → uf:            {uf_antes:5.1f}% → {uf_dep:5.1f}%")
    print(f"  → lance_inicial: {lance_antes:5.1f}% → {lance_dep:5.1f}%")
    con.close()


def auditar(db_path, corrigir=False, limite_mostra=40, csv_out=None):
    """Audita as UFs JÁ preenchidas: re-infere do texto (alta precisão) e sinaliza
    divergências (provável UF errada vinda do scraper). Classifica por confiança:

      ALTA  — o próprio campo `cidade` (município de UF única) contradiz a UF salva;
              é seguro corrigir (ex.: cidade=Joinville salvo como SE → SC).
      BAIXA — só o título/descrição divergem (pode ser lote multi-localização); só reporta.

    Por padrão só relata; com corrigir=True sobrescreve APENAS as de confiança ALTA.
    """
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    alta, baixa = [], []
    for r in con.execute("SELECT id, titulo, descricao, endereco, cidade, uf, url "
                          "FROM imoveis WHERE uf IS NOT NULL AND TRIM(uf) <> ''"):
        atual = r["uf"].strip().upper()
        inf = sc.inferir_uf(r["titulo"], r["endereco"], r["descricao"], r["cidade"])
        if not inf or inf == atual:
            continue
        item = {"id": r["id"], "atual": atual, "inferida": inf,
                "cidade": r["cidade"] or "", "url": r["url"] or "",
                "titulo": (r["titulo"] or "")[:120],
                "txt": ((r["titulo"] or "") + " ¦ " + (r["cidade"] or "") + " ¦ "
                        + (r["descricao"] or "")).strip()[:88]}
        uf_cidade = sc.inferir_uf(r["cidade"]) if (r["cidade"] or "").strip() else None
        item["confianca"] = "alta" if (uf_cidade and uf_cidade == inf and uf_cidade != atual) else "baixa"
        (alta if item["confianca"] == "alta" else baixa).append(item)

    total = con.execute("SELECT COUNT(*) FROM imoveis WHERE uf IS NOT NULL "
                        "AND TRIM(uf) <> ''").fetchone()[0]
    print(f"  UFs preenchidas: {total} | divergências: {len(alta) + len(baixa)}  "
          f"(ALTA confiança: {len(alta)} · baixa: {len(baixa)})")
    print(f"\n  ALTA confiança (campo cidade contradiz a UF salva — seguro corrigir):")
    for d in alta[:limite_mostra]:
        print(f"    {d['atual']} → {d['inferida']}   {d['txt']}")
    if len(alta) > limite_mostra:
        print(f"    ... (+{len(alta) - limite_mostra})")
    if baixa:
        print(f"\n  baixa confiança (só título/descrição; revisar à mão — NÃO corrigido):")
        for d in baixa[:10]:
            print(f"    {d['atual']} → {d['inferida']}   {d['txt']}")
        if len(baixa) > 10:
            print(f"    ... (+{len(baixa) - 10})")

    if corrigir and alta:
        con.executemany("UPDATE imoveis SET uf = ? WHERE id = ?",
                        [(d["inferida"], d["id"]) for d in alta])
        con.commit()
        print(f"\n  ✔ {len(alta)} UFs de ALTA confiança corrigidas. "
              f"(baixa confiança preservada para revisão)")
    elif alta:
        print("\n  (relatório; use --corrigir para aplicar só as de ALTA confiança)")

    if csv_out:
        import csv as _csv
        with open(csv_out, "w", newline="", encoding="utf-8") as f:
            w = _csv.DictWriter(f, fieldnames=["id", "confianca", "uf_atual",
                                "uf_inferida", "decisao", "cidade", "titulo", "url"])
            w.writeheader()
            for d in alta + baixa:
                w.writerow({"id": d["id"], "confianca": d["confianca"],
                            "uf_atual": d["atual"], "uf_inferida": d["inferida"],
                            "decisao": "",  # preencha: aplicar | manter | <UF> (ex.: RJ)
                            "cidade": d["cidade"], "titulo": d["titulo"], "url": d["url"]})
        print(f"\n  → {csv_out}: {len(alta) + len(baixa)} divergências para revisão.")
    con.close()
    return {"alta": alta, "baixa": baixa}


def auditar_cidade(db_path, limite_mostra=25):
    """Audita o campo `cidade` contra o IBGE: (a) cidades INEXISTENTES (typo/lixo) e
    (b) cidades que existem mas NÃO na UF salva (cidade/uf inconsistente)."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    inexistentes, uf_inconsistente = [], []
    total = 0
    for r in con.execute("SELECT cidade, uf, COUNT(*) n FROM imoveis "
                         "WHERE cidade IS NOT NULL AND TRIM(cidade) <> '' "
                         "GROUP BY cidade, uf"):
        total += r["n"]
        cidade, uf = r["cidade"].strip(), (r["uf"] or "").strip().upper()
        if not sc.municipio_valido(cidade):
            inexistentes.append((cidade, uf, r["n"]))
        elif uf and not sc.municipio_valido(cidade, uf):
            uf_inconsistente.append((cidade, uf, r["n"]))
    print(f"  Linhas com cidade: {total}")
    print(f"\n  (a) cidade INEXISTENTE no IBGE (typo/ruído): "
          f"{sum(n for *_, n in inexistentes)} linhas, {len(inexistentes)} valores distintos")
    for cidade, uf, n in sorted(inexistentes, key=lambda x: -x[2])[:limite_mostra]:
        print(f"      {n:>4}×  {cidade!r}  (uf={uf or '-'})")
    print(f"\n  (b) cidade existe mas NÃO na UF salva (cidade/uf inconsistente): "
          f"{sum(n for *_, n in uf_inconsistente)} linhas, {len(uf_inconsistente)} distintos")
    for cidade, uf, n in sorted(uf_inconsistente, key=lambda x: -x[2])[:limite_mostra]:
        print(f"      {n:>4}×  {cidade!r} salvo como {uf}")
    con.close()
    return {"inexistentes": inexistentes, "uf_inconsistente": uf_inconsistente}


def limpar_cidade(db_path, dry_run=False, mostra=25):
    """Limpa o campo `cidade` inválido (bairro+município concatenado, ruído): extrai o
    município real via IBGE (sc.extrair_municipio, usando a UF salva p/ desambiguar) e o
    substitui pelo nome canônico. Preenche `uf` vazia quando a extração resolve a UF.
    Linhas sem município reconhecível ficam inalteradas (não apaga dado)."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    invalidas_antes = sum(
        1 for r in con.execute("SELECT cidade FROM imoveis "
                               "WHERE cidade IS NOT NULL AND TRIM(cidade) <> ''")
        if not sc.municipio_valido(r["cidade"])
    )
    upd, exemplos, irrecuperaveis, conflitos = [], [], 0, 0
    for r in con.execute("SELECT id, cidade, uf FROM imoveis "
                          "WHERE cidade IS NOT NULL AND TRIM(cidade) <> ''"):
        if sc.municipio_valido(r["cidade"]):
            continue
        uf_atual = (r["uf"] or "").strip().upper()
        m = sc.extrair_municipio(r["cidade"], uf_hint=uf_atual or None)
        if not m:
            irrecuperaveis += 1
            continue
        # Guarda de consistência: se há UF salva e o município extraído NÃO existe nela,
        # é match suspeito (homônimo / grafia divergente, ex.: 'Mirassol' vs "Mirassol d'Oeste")
        # → não altera, p/ não criar registro cidade/uf incoerente.
        if uf_atual and uf_atual not in m["ufs"]:
            conflitos += 1
            continue
        nova_uf = m["uf"] if (not uf_atual and m["uf"]) else uf_atual
        upd.append((m["nome"], nova_uf, r["id"]))
        if len(exemplos) < mostra:
            exemplos.append((r["cidade"], m["nome"], nova_uf))

    print(f"  cidades inválidas: {invalidas_antes} | recuperáveis: {len(upd)} | "
          f"conflito uf (pulado): {conflitos} | irrecuperáveis (ruído): {irrecuperaveis}")
    for orig, novo, uf in exemplos:
        print(f"    {orig!r:42} → {novo!r} ({uf})")
    if len(upd) > mostra:
        print(f"    ... (+{len(upd) - mostra})")

    if dry_run:
        print("  [dry-run] nada gravado.")
    else:
        con.executemany("UPDATE imoveis SET cidade = ?, uf = ? WHERE id = ?", upd)
        con.commit()
        validas = sum(
            1 for r in con.execute("SELECT cidade FROM imoveis "
                                   "WHERE cidade IS NOT NULL AND TRIM(cidade) <> ''")
            if sc.municipio_valido(r["cidade"])
        )
        tot = con.execute("SELECT COUNT(*) FROM imoveis WHERE cidade IS NOT NULL "
                          "AND TRIM(cidade) <> ''").fetchone()[0]
        print(f"  ✔ {len(upd)} cidades normalizadas. Válidas no IBGE: "
              f"{100*validas/tot:.1f}% das preenchidas.")
    con.close()
    return upd


def main():
    ap = argparse.ArgumentParser(description="Enriquecimento offline de uf/lance_inicial.")
    ap.add_argument("--db", default=DB_PADRAO)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--auditar", action="store_true",
                    help="audita UFs já preenchidas vs. inferência (não enriquece vazios)")
    ap.add_argument("--corrigir", action="store_true",
                    help="com --auditar: sobrescreve UFs divergentes pela inferência")
    ap.add_argument("--csv", default=None,
                    help="com --auditar: exporta as divergências para revisão (ex.: uf_revisao.csv)")
    ap.add_argument("--auditar-cidade", action="store_true",
                    help="audita o campo cidade contra o IBGE (inexistentes / uf inconsistente)")
    ap.add_argument("--limpar-cidade", action="store_true",
                    help="extrai o município real de cidades inválidas (bairro+cidade/ruído)")
    args = ap.parse_args()
    if args.limpar_cidade:
        print("LIMPEZA DE CIDADE (extrai município do IBGE)")
        limpar_cidade(args.db, dry_run=args.dry_run)
        return
    if args.auditar_cidade:
        print("AUDITORIA DE CIDADE (contra _ibge_municipios.json)")
        auditar_cidade(args.db)
        return
    if args.auditar:
        print("AUDITORIA DE UF (existente vs. inferida de alta precisão)")
        auditar(args.db, corrigir=args.corrigir, csv_out=args.csv)
        return
    print("ENRICH LOCAL (texto já no banco)")
    enriquecer(args.db, args.dry_run)


if __name__ == "__main__":
    main()
