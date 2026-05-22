#!/usr/bin/env python3
"""
Gerador de RSS – Normas da Receita Federal no DOU
Endpoint real: https://in.gov.br/leiturajornal?org=...&org_sub=...

Como funciona:
  1. Lê last_run.txt para saber quando rodou pela última vez
  2. Busca atos da RFB no DOU publicados desde então
  3. Gera docs/feed.xml e atualiza last_run.txt
  4. O GitHub Actions commita tudo e faz deploy no GitHub Pages
  5. O InoReader assina: https://smrai74.github.io/normas-rfb-rss/feed.xml
"""

import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom

# ── configurações ──────────────────────────────────────────────────────────────

# Fallback: quantos dias buscar se last_run.txt não existir (primeira execução)
DIAS_FALLBACK = 1

# Órgão e subórgão exatos como aparecem no DOU
ORG     = "Ministério da Fazenda"
ORG_SUB = "Secretaria Especial da Receita Federal do Brasil"

# Máximo de itens no feed
MAX_ITENS = 50

# URL do seu feed — atualize com seu usuário e repositório do GitHub
FEED_URL = "https://smrai74.github.io/normas-rfb-rss/feed.xml"

# Arquivos
SAIDA    = Path("docs/feed.xml")
LAST_RUN = Path("last_run.txt")

# ── headers de browser ─────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Referer": "https://in.gov.br/",
}

# ── funções ────────────────────────────────────────────────────────────────────

def buscar_leiturajornal(data_str: str) -> list[dict]:
    """
    Busca atos da RFB usando o endpoint real do DOU filtrado por órgão.
    data_str formato: DD-MM-YYYY
    Tenta até 3 vezes com delay entre tentativas.
    """
    params = urlencode({
        "org":     ORG,
        "org_sub": ORG_SUB,
        "data":    data_str,
    })
    url = f"https://in.gov.br/leiturajornal?{params}"
    
    max_tentativas = 3
    for tentativa in range(1, max_tentativas + 1):
        print(f"  GET {url} (tentativa {tentativa}/{max_tentativas})")

        try:
            req = Request(url, headers=HEADERS)
            with urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
        except HTTPError as e:
            if e.code == 502 and tentativa < max_tentativas:
                print(f"  Servidor retornou 502. Aguardando 10s antes de tentar novamente…", file=sys.stderr)
                time.sleep(10)
                continue
            print(f"  Erro HTTP {e.code} ({data_str}): {e}", file=sys.stderr)
            return []
        except URLError as e:
            print(f"  Erro de rede ({data_str}): {e}", file=sys.stderr)
            return []

        # Parsing bem-sucedido
        break
    else:
        print(f"  Falhou após {max_tentativas} tentativas", file=sys.stderr)
        return []

    # Tenta parse direto como JSON
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Resposta é HTML — extrai JSON embutido em <script type="application/json">
        match = re.search(
            r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
            raw, re.DOTALL
        )
        if not match:
            print(f"  Sem dados JSON na resposta de {data_str}", file=sys.stderr)
            return []
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError as e:
            print(f"  JSON inválido ({data_str}): {e}", file=sys.stderr)
            return []

    # Normaliza: pode ser lista direta ou dict com chave "jsonArray" / "content"
    if isinstance(data, list):
        items = data
    else:
        items = data.get("jsonArray") or data.get("content") or []

    artigos = []
    for item in items:
        url_title = item.get("urlTitle", "")
        if not url_title:
            continue
        artigos.append({
            "url":      f"https://www.in.gov.br/en/web/dou/-/{url_title}",
            "titulo":   limpar_html(item.get("title") or item.get("titulo") or ""),
            "orgao":    item.get("orgao") or item.get("subOrgao") or ORG_SUB,
            "pub_date": item.get("pubDate") or item.get("dataPublicacao") or data_str,
            "resumo":   limpar_html(
                item.get("excerpt") or item.get("content") or item.get("ementa") or ""
            ),
        })

    print(f"  {len(artigos)} atos encontrados para {data_str}")
    
    # Pequeno delay entre requisições para não sobrecarregar o servidor
    time.sleep(2)
    return artigos


def ler_last_run() -> datetime:
    """Lê a data/hora da última execução. Usa fallback se last_run.txt não existir."""
    if LAST_RUN.exists():
        try:
            dt = datetime.fromisoformat(LAST_RUN.read_text().strip())
            print(f"  Última execução: {dt.strftime('%d/%m/%Y %H:%M')}")
            return dt
        except ValueError:
            pass
    fallback = datetime.now() - timedelta(days=DIAS_FALLBACK)
    print(f"  last_run.txt ausente — buscando últimas {DIAS_FALLBACK * 24}h")
    return fallback


def salvar_last_run():
    LAST_RUN.write_text(datetime.now().isoformat())


def coletar_artigos() -> list[dict]:
    """Coleta atos publicados desde a última execução."""
    ultima = ler_last_run()
    hoje   = datetime.now()
    dias   = (hoje.date() - ultima.date()).days + 1

    print(f"  Cobrindo {dias} dia(s): {ultima.strftime('%d/%m/%Y')} → {hoje.strftime('%d/%m/%Y')}")

    vistos: set[str] = set()
    resultado: list[dict] = []

    for d in range(dias):
        data_str = (hoje - timedelta(days=d)).strftime("%d-%m-%Y")
        for art in buscar_leiturajornal(data_str):
            if art["url"] not in vistos:
                vistos.add(art["url"])
                resultado.append(art)
        if len(resultado) >= MAX_ITENS:
            break

    resultado.sort(key=lambda x: x.get("pub_date", ""), reverse=True)
    return resultado[:MAX_ITENS]


def limpar_html(texto: str) -> str:
    if not texto:
        return ""
    texto = re.sub(r"<[^>]+>", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def data_para_rfc822(raw: str) -> str:
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(raw[:19].strip(), fmt)
            return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except (ValueError, TypeError):
            continue
    return datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")


def gerar_rss(artigos: list[dict]) -> str:
    rss = Element("rss", version="2.0")
    rss.set("xmlns:atom", "http://www.w3.org/2005/Atom")

    ch = SubElement(rss, "channel")
    SubElement(ch, "title").text       = "Normas RFB – Diário Oficial da União"
    SubElement(ch, "link").text        = "https://www.in.gov.br/consulta"
    SubElement(ch, "description").text = (
        "Atos normativos da Receita Federal do Brasil publicados no DOU – "
        "atualizado automaticamente via GitHub Actions"
    )
    SubElement(ch, "language").text      = "pt-BR"
    SubElement(ch, "lastBuildDate").text = datetime.utcnow().strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )
    al = SubElement(ch, "atom:link")
    al.set("href", FEED_URL)
    al.set("rel",  "self")
    al.set("type", "application/rss+xml")

    if not artigos:
        it = SubElement(ch, "item")
        SubElement(it, "title").text   = "Nenhum ato publicado no período"
        SubElement(it, "link").text    = "https://www.in.gov.br/consulta"
        SubElement(it, "pubDate").text = datetime.utcnow().strftime(
            "%a, %d %b %Y %H:%M:%S +0000"
        )
        return _xml(rss)

    for art in artigos:
        it = SubElement(ch, "item")
        SubElement(it, "title").text                    = art.get("titulo") or "Sem título"
        SubElement(it, "link").text                     = art["url"]
        SubElement(it, "guid", isPermaLink="true").text = art["url"]
        SubElement(it, "pubDate").text                  = data_para_rfc822(art.get("pub_date", ""))
        if art.get("orgao"):
            SubElement(it, "author").text = art["orgao"]
        resumo = art.get("resumo", "")
        if resumo:
            SubElement(it, "description").text = resumo[:800] + ("…" if len(resumo) > 800 else "")

    return _xml(rss)


def _xml(elem: Element) -> str:
    raw = tostring(elem, encoding="unicode")
    return minidom.parseString(raw).toprettyxml(indent="  ", encoding=None)


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    print("=== Gerador RSS – Normas RFB ===")
    print(f"Órgão: {ORG_SUB}")

    artigos = coletar_artigos()
    print(f"Total: {len(artigos)} atos encontrados")

    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(gerar_rss(artigos), encoding="utf-8")
    print(f"Feed salvo em: {SAIDA}")

    salvar_last_run()
    print(f"last_run.txt atualizado: {datetime.now().strftime('%d/%m/%Y %H:%M')}")


if __name__ == "__main__":
    main()
