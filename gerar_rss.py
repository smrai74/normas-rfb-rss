#!/usr/bin/env python3
"""
Gerador de RSS – Normas da Receita Federal no DOU
https://github.com/smrai74/normas-rfb-rss
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

DIAS_FALLBACK = 1
ORG     = "Ministério da Fazenda"
ORG_SUB = "Secretaria Especial da Receita Federal do Brasil"
MAX_ITENS = 50
FEED_URL = "https://smrai74.github.io/normas-rfb-rss/feed.xml"
SAIDA    = Path("docs/feed.xml")
LAST_RUN = Path("last_run.txt")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Connection": "keep-alive",
}


def buscar_leiturajornal(data_str: str) -> list[dict]:
    """Busca atos da RFB no DOU filtrado por órgão."""
    params = urlencode({"org": ORG, "org_sub": ORG_SUB, "data": data_str})
    url = f"https://in.gov.br/leiturajornal?{params}"
    print(f"  Buscando: {data_str}")

    try:
        req = Request(url, headers=HEADERS)
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:
        print(f"    Erro: {e}", file=sys.stderr)
        return []

    # Extrai JSON da resposta
    try:
        data = json.loads(raw)
    except:
        match = re.search(r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>', raw, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(1))
        except:
            return []

    # Normaliza items
    if isinstance(data, list):
        items = data
    else:
        items = data.get("jsonArray") or data.get("content") or data.get("atos") or []

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
            "resumo":   limpar_html(item.get("excerpt") or item.get("content") or item.get("ementa") or ""),
        })

    print(f"    {len(artigos)} atos encontrados")
    time.sleep(1)
    return artigos


def ler_last_run() -> datetime:
    """Lê last_run.txt ou usa fallback."""
    if LAST_RUN.exists():
        try:
            return datetime.fromisoformat(LAST_RUN.read_text().strip())
        except:
            pass
    return datetime.now() - timedelta(days=DIAS_FALLBACK)


def salvar_last_run():
    """Salva timestamp da execução."""
    LAST_RUN.write_text(datetime.now().isoformat())


def coletar_artigos() -> list[dict]:
    """Coleta atos desde a última execução."""
    ultima = ler_last_run()
    hoje = datetime.now()
    dias = (hoje.date() - ultima.date()).days + 1

    vistos = set()
    resultado = []

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
    return re.sub(r"<[^>]+>", " ", texto).strip()[:300]


def data_para_rfc822(raw: str) -> str:
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(raw[:19].strip(), fmt)
            return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except:
            pass
    return datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")


def gerar_rss(artigos: list[dict]) -> str:
    rss = Element("rss", version="2.0")
    rss.set("xmlns:atom", "http://www.w3.org/2005/Atom")

    ch = SubElement(rss, "channel")
    SubElement(ch, "title").text = "Normas RFB – Diário Oficial da União"
    SubElement(ch, "link").text = "https://www.in.gov.br/consulta"
    SubElement(ch, "description").text = "Atos normativos da Receita Federal do Brasil publicados no DOU"
    SubElement(ch, "language").text = "pt-BR"
    SubElement(ch, "lastBuildDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
    
    al = SubElement(ch, "atom:link")
    al.set("href", FEED_URL)
    al.set("rel", "self")
    al.set("type", "application/rss+xml")

    if not artigos:
        it = SubElement(ch, "item")
        SubElement(it, "title").text = "Nenhum ato publicado"
        SubElement(it, "link").text = "https://www.in.gov.br/"
        SubElement(it, "pubDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
    else:
        for art in artigos:
            it = SubElement(ch, "item")
            SubElement(it, "title").text = art.get("titulo") or "Sem título"
            SubElement(it, "link").text = art["url"]
            SubElement(it, "guid", isPermaLink="true").text = art["url"]
            SubElement(it, "pubDate").text = data_para_rfc822(art.get("pub_date", ""))
            if art.get("orgao"):
                SubElement(it, "author").text = art["orgao"]
            resumo = art.get("resumo", "")
            if resumo:
                SubElement(it, "description").text = resumo

    raw = tostring(rss, encoding="unicode")
    return minidom.parseString(raw).toprettyxml(indent="  ", encoding=None)


def main():
    print("=== Gerador RSS – Normas RFB ===")
    artigos = coletar_artigos()
    print(f"Total: {len(artigos)} atos")

    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(gerar_rss(artigos), encoding="utf-8")
    print(f"Feed salvo: {SAIDA}")

    salvar_last_run()
    print("Concluído!")


if __name__ == "__main__":
    main()
