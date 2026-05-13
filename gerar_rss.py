#!/usr/bin/env python3
"""
Gerador de RSS – Normas da Receita Federal no DOU
Consulta a API da Imprensa Nacional e gera docs/feed.xml

Como funciona:
  1. Lê last_run.txt para saber quando rodou pela última vez
  2. Busca atos da RFB publicados desde então
  3. Gera docs/feed.xml e salva a hora atual em last_run.txt
  4. O GitHub Actions commita tudo e faz deploy no GitHub Pages
  5. O InoReader assina: https://<usuario>.github.io/<repo>/feed.xml
"""

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom

# ── configurações ──────────────────────────────────────────────────────────────

# Fallback: quantos dias buscar se last_run.txt não existir (primeira execução)
DIAS_FALLBACK = 1

# Seções do DOU: "do1" = Seção 1 (atos normativos), "do1_extra" = edições extras
SECOES = ["do1", "do1_extra"]

# Órgão exato como aparece no DOU
ORGAO = "Receita Federal do Brasil"

# Máximo de itens no feed
MAX_ITENS = 50

# URL do seu feed (atualize após criar o repositório)
FEED_URL = "https://SEU_USUARIO.github.io/SEU_REPO/feed.xml"

# Arquivos
SAIDA      = Path("docs/feed.xml")
LAST_RUN   = Path("last_run.txt")

# ── funções ────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.in.gov.br/consulta",
    "Accept-Language": "pt-BR,pt;q=0.9",
}


def buscar_edicao_do_dia(data_str: str, secao: str) -> list[str]:
    """
    Busca os urlTitles de todos os artigos de uma edição do DOU.
    Endpoint: https://www.in.gov.br/leiturajornal?data=DD-MM-YYYY&secao=do1
    Retorna lista de urlTitle strings.
    """
    url = f"https://www.in.gov.br/leiturajornal?data={data_str}&secao={secao}"
    try:
        req = Request(url, headers=HEADERS)
        with urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8")
        # O DOU embute os dados como JSON em uma tag <script type="application/json">
        match = re.search(
            r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL
        )
        if not match:
            return []
        data = json.loads(match.group(1))
        items = data.get("jsonArray", [])
        return [i.get("urlTitle", "") for i in items if i.get("urlTitle")]
    except (URLError, json.JSONDecodeError, KeyError) as e:
        print(f"  Aviso leiturajornal ({secao}/{data_str}): {e}", file=sys.stderr)
        return []


def buscar_conteudo_artigo(url_title: str) -> dict:
    """
    Busca o conteúdo completo de um artigo pelo seu urlTitle.
    Endpoint: https://www.in.gov.br/en/web/dou/-/<urlTitle>
    Retorna dict com campos do artigo ou dict vazio em caso de erro.
    """
    url = f"https://www.in.gov.br/en/web/dou/-/{url_title}"
    try:
        req = Request(url, headers=HEADERS)
        with urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8")

        # Extrai metadados do JSON embutido na página
        match = re.search(
            r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL
        )
        if match:
            data = json.loads(match.group(1))
            return {
                "url": url,
                "url_title": url_title,
                "titulo": limpar_html(data.get("title", "")),
                "orgao": data.get("orgao", ""),
                "pub_date": data.get("pubDate", ""),
                "resumo": limpar_html(data.get("content", data.get("excerpt", ""))),
            }

        # Fallback: extrai via HTML simples
        titulo = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.DOTALL)
        return {
            "url": url,
            "url_title": url_title,
            "titulo": limpar_html(titulo.group(1)) if titulo else url_title,
            "orgao": ORGAO,
            "pub_date": "",
            "resumo": "",
        }
    except (URLError, json.JSONDecodeError) as e:
        print(f"  Aviso artigo ({url_title}): {e}", file=sys.stderr)
        return {}


def buscar_via_pesquisa(data_inicio: str, data_fim: str) -> list[dict]:
    """
    Alternativa: usa o endpoint de busca do DOU filtrado por órgão.
    Retorna lista de dicts com dados dos artigos.
    """
    orgao_encoded = ORGAO.replace(" ", "+")
    url = (
        "https://www.in.gov.br/consulta/-/buscar/dou"
        f"?q=%22{orgao_encoded}%22"
        f"&s=do1,do1_extra"
        f"&exactDate=personalizado"
        f"&startDate={data_inicio}"
        f"&endDate={data_fim}"
        f"&orgaosSelecionados={orgao_encoded}"
        "&score=0&size=20&sortType=0"
    )
    try:
        req = Request(url, headers=HEADERS)
        with urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        artigos = []
        for item in data.get("content", []):
            artigos.append({
                "url": f"https://www.in.gov.br/en/web/dou/-/{item.get('urlTitle','')}",
                "url_title": item.get("urlTitle", ""),
                "titulo": limpar_html(item.get("title", "")),
                "orgao": item.get("orgao", ORGAO),
                "pub_date": item.get("pubDate", ""),
                "resumo": limpar_html(item.get("excerpt", item.get("content", ""))),
            })
        return artigos
    except (URLError, json.JSONDecodeError) as e:
        print(f"  Aviso busca: {e}", file=sys.stderr)
        return []


def ler_last_run() -> datetime:
    """Lê a data/hora da última execução de last_run.txt. Usa fallback se não existir."""
    if LAST_RUN.exists():
        try:
            ts = LAST_RUN.read_text().strip()
            dt = datetime.fromisoformat(ts)
            print(f"  Última execução: {dt.strftime('%d/%m/%Y %H:%M')}")
            return dt
        except ValueError:
            pass
    fallback = datetime.now() - timedelta(days=DIAS_FALLBACK)
    print(f"  last_run.txt não encontrado. Usando fallback: últimas {DIAS_FALLBACK*24}h")
    return fallback


def salvar_last_run():
    """Salva a data/hora atual em last_run.txt."""
    LAST_RUN.write_text(datetime.now().isoformat())


def coletar_artigos() -> list[dict]:
    """
    Busca atos publicados desde a última execução.
    Tenta primeiro o endpoint de pesquisa; se falhar, lê edição por edição.
    """
    ultima = ler_last_run()
    hoje = datetime.now()

    # Gera lista de datas a cobrir (da última execução até hoje)
    dias_diff = (hoje.date() - ultima.date()).days + 1
    dt_inicio = ultima.strftime("%d-%m-%Y")
    dt_fim = hoje.strftime("%d-%m-%Y")

    print(f"  Buscando de {dt_inicio} até {dt_fim} ({dias_diff} dia(s))…")
    artigos = buscar_via_pesquisa(dt_inicio, dt_fim)

    if artigos:
        print(f"  Encontrados {len(artigos)} artigos via pesquisa.")
        rfb = [a for a in artigos if ORGAO.lower() in a.get("orgao", "").lower()]
        resultado = rfb if rfb else artigos
    else:
        print("  Pesquisa falhou. Lendo edições do período…")
        vistos: set[str] = set()
        resultado = []
        for dias_atras in range(dias_diff):
            data_str = (hoje - timedelta(days=dias_atras)).strftime("%d-%m-%Y")
            for secao in SECOES:
                print(f"  Lendo edição {data_str} / {secao}…")
                url_titles = buscar_edicao_do_dia(data_str, secao)
                for ut in url_titles:
                    if ut in vistos:
                        continue
                    vistos.add(ut)
                    art = buscar_conteudo_artigo(ut)
                    if art and ORGAO.lower() in art.get("orgao", "").lower():
                        resultado.append(art)
                    if len(resultado) >= MAX_ITENS:
                        break
                if len(resultado) >= MAX_ITENS:
                    break
            if len(resultado) >= MAX_ITENS:
                break

    resultado.sort(key=lambda x: x.get("pub_date", ""), reverse=True)
    return resultado[:MAX_ITENS]


def limpar_html(texto: str) -> str:
    if not texto:
        return ""
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = re.sub(r"\s+", " ", texto)
    return texto.strip()


def data_para_rfc822(raw: str) -> str:
    """Converte 'dd/MM/yyyy HH:mm:ss' ou 'dd-MM-yyyy' para RFC 822."""
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(raw[:len(fmt) + 2].strip(), fmt)
            return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except (ValueError, TypeError):
            continue
    return datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0000")


def gerar_rss(artigos: list[dict]) -> str:
    rss = Element("rss", version="2.0")
    rss.set("xmlns:atom", "http://www.w3.org/2005/Atom")

    ch = SubElement(rss, "channel")
    SubElement(ch, "title").text = "Normas RFB – Diário Oficial da União"
    SubElement(ch, "link").text = "https://www.in.gov.br/consulta"
    SubElement(ch, "description").text = (
        "Atos normativos da Receita Federal do Brasil publicados no DOU – "
        "atualizado automaticamente via GitHub Actions"
    )
    SubElement(ch, "language").text = "pt-BR"
    SubElement(ch, "lastBuildDate").text = datetime.utcnow().strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )
    al = SubElement(ch, "atom:link")
    al.set("href", FEED_URL)
    al.set("rel", "self")
    al.set("type", "application/rss+xml")

    if not artigos:
        it = SubElement(ch, "item")
        SubElement(it, "title").text = "Nenhum resultado encontrado no período"
        SubElement(it, "link").text = "https://www.in.gov.br/consulta"
        SubElement(it, "pubDate").text = datetime.utcnow().strftime(
            "%a, %d %b %Y %H:%M:%S +0000"
        )
        return _xml(rss)

    for art in artigos:
        it = SubElement(ch, "item")
        SubElement(it, "title").text = art.get("titulo") or "Sem título"
        SubElement(it, "link").text = art.get("url", "https://www.in.gov.br/consulta")
        SubElement(it, "guid", isPermaLink="true").text = art.get(
            "url", "https://www.in.gov.br/consulta"
        )
        SubElement(it, "pubDate").text = data_para_rfc822(art.get("pub_date", ""))
        if art.get("orgao"):
            SubElement(it, "author").text = art["orgao"]
        resumo = art.get("resumo", "")
        if resumo:
            SubElement(it, "description").text = (
                resumo[:800] + "…" if len(resumo) > 800 else resumo
            )

    return _xml(rss)


def _xml(elem: Element) -> str:
    raw = tostring(elem, encoding="unicode")
    return minidom.parseString(raw).toprettyxml(indent="  ", encoding=None)


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    print("=== Gerador RSS – Normas RFB ===")
    print(f"Órgão: {ORGAO}")

    artigos = coletar_artigos()
    print(f"Total final: {len(artigos)} artigos da RFB")

    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(gerar_rss(artigos), encoding="utf-8")
    print(f"Feed salvo: {SAIDA}")

    salvar_last_run()
    print(f"last_run.txt atualizado: {datetime.now().strftime('%d/%m/%Y %H:%M')}")


if __name__ == "__main__":
    main()
