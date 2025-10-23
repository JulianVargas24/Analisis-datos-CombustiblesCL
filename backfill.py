# backfill.py
from __future__ import annotations

import re
import time
import sys
import datetime as dt
import re, unicodedata, datetime as dt
import fitz  # PyMuPDF

NUM = r'([+-]?\d+(?:,\d)?)'         # número con coma, opcional signo
PESOLT = r'\$?\s*/\s*lt\.?'         # $/lt, con o sin punto
WS = r'(?:\s| |&nbsp;)+'           # espacios “duros”

MESES = {
    "enero":1, "febrero":2, "marzo":3, "abril":4, "mayo":5, "junio":6,
    "julio":7, "agosto":8, "septiembre":9, "setiembre":9, "octubre":10,
    "noviembre":11, "diciembre":12
}

def _norm_text(t: str) -> str:
    # quita tildes, colapsa espacios y pasa a minúsculas
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = re.sub(r'\s+', ' ', t).strip().lower()
    return t

def _first_num(s: str):
    m = re.search(NUM, s)
    return float(m.group(1).replace(',', '.')) if m else None

from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import pandas as pd
import fitz  # PyMuPDF
from bs4 import BeautifulSoup
from urllib.parse import urljoin


# =========================
# Config
# =========================
BASE = "https://www.enap.cl"
# Usamos la URL de la primera página; las demás se descubren desde la paginación
ARCHIVE_FIRST = "https://www.enap.cl/archivos/8/informe-semanal-de-precios?year={year}&page=0"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
CSV_PATH = DATA_DIR / "variaciones_semana.csv"


# =========================
# Utilidades HTML / paginación
# =========================
def _collect_pdf_links_from_page_html(html: str) -> Tuple[List[str], BeautifulSoup]:
    soup = BeautifulSoup(html, "html.parser")
    hrefs: List[str] = []
    for a in soup.select('a[href*="/files/get/"]'):
        h = a.get("href")
        if not h:
            continue
        hrefs.append(h if h.startswith("http") else urljoin(BASE, h))
    return hrefs, soup


def _extract_pagination_urls(soup: BeautifulSoup, year: int) -> Dict[int, str]:
    """
    De la paginación real del sitio, saca {page_n: href_absoluto}.
    """
    pages: Dict[int, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "informe-semanal-de-precios" in href and f"year={year}" in href and "page=" in href:
            m = re.search(r"page=(\d+)", href)
            if m:
                n = int(m.group(1))
                pages[n] = href if href.startswith("http") else urljoin(BASE, href)
    return pages


def list_pdfs_for_year(year: int, sleep_s: float = 0.45) -> List[str]:
    """
    Descubre la paginación leyendo la primera página y sigue los HREFs reales.
    Devuelve TODAS las URLs de PDFs del año.
    """
    seen = set()
    out: List[str] = []

    # 1) cargar page=0
    first_url = ARCHIVE_FIRST.format(year=year)
    r = requests.get(first_url, headers=HEADERS, timeout=30)
    r.raise_for_status()

    pdfs0, soup0 = _collect_pdf_links_from_page_html(r.text)
    for u in pdfs0:
        if u not in seen:
            seen.add(u); out.append(u)
    print(f"[list] {year} page=0 -> {len(pdfs0)} nuevos (total={len(out)})")

    # 2) extraer todas las URLs de paginación reales
    page_urls = _extract_pagination_urls(soup0, year)
    if not page_urls:
        return out

    max_page = max(page_urls)
    # 3) recorrer 1..max_page usando los hrefs tal cual
    for page in range(1, max_page + 1):
        url = page_urls.get(page)
        if not url:
            print(f"[list] {year} page={page} -> href no encontrado (saltando)")
            continue
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        pdfs, _ = _collect_pdf_links_from_page_html(r.text)
        add = 0
        for u in pdfs:
            if u not in seen:
                seen.add(u); out.append(u); add += 1
        print(f"[list] {year} page={page} -> {add} nuevos (total={len(out)})")
        time.sleep(sleep_s)

    return out


# =========================
# Descarga de PDFs
# =========================
def download_pdf(url: str, out_dir: Path = Path("tmp")) -> Path:
    """
    Descarga un PDF y devuelve su ruta local.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", url.split("/")[-1])
    dest = out_dir / name
    if not dest.exists():
        r = requests.get(url, headers=HEADERS, timeout=60)
        r.raise_for_status()
        dest.write_bytes(r.content)
        time.sleep(0.25)
    return dest


# =========================
# Parseo de PDF
# =========================
MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9,
    "octubre": 10, "noviembre": 11, "diciembre": 12
}

NUM_PATS = [
    r"([+-]?\d+,\d)\s*\$/lt",
    r"([+-]?\d+\.\d)\s*\$/lt",
    r"([+-]?\d+,\d)\s*\$",
    r"([+-]?\d+\.\d)\s*\$",
    r"([+-]?\d+,\d)",
    r"([+-]?\d+\.\d)",
]

def _first_number(s: str) -> Optional[float]:
    for p in NUM_PATS:
        m = re.search(p, s, re.I)
        if m:
            return float(m.group(1).replace(",", "."))
    return None


def parse_variations(pdf_path: Path):
    """
    Devuelve dict con:
      {'fecha','gasolina_93','gasolina_97','diesel','kerosene','glp'}
    Si no puede leer la fecha o no encuentra ningún número, devuelve None.
    NO exige que estén los 5 combustibles para aceptar la fila.
    """
    try:
        pages = []
        with fitz.open(pdf_path) as doc:
            for p in doc:
                pages.append(p.get_text("text"))
        text_raw = "\n".join(pages)
    except Exception:
        return None

    if not text_raw or len(text_raw) < 40:
        return None

    text_norm = _norm_text(text_raw)

    # -------- fecha ----------
    f = re.search(r'(\d{1,2}) de (enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre) de (\d{4})',
                  text_norm)
    if not f:
        return None
    d, mon, y = int(f.group(1)), MESES[f.group(2)], int(f.group(3))
    fecha = dt.date(y, mon, d).isoformat()

    out = {"fecha": fecha,
           "gasolina_93": None, "gasolina_97": None,
           "diesel": None, "kerosene": None, "glp": None}

    # -------- mapeo de sinónimos ----------
    keys = {
        "gasolina_93": [r'gasolina.*93'],
        "gasolina_97": [r'gasolina.*97'],
        "diesel":      [r'dies[e]?l'],              # diesel/diésel
        "kerosene":    [r'kerosen', r'parafina'],   # kerosene/kerosén/parafina
        "glp":         [r'\bglp\b', r'gas licuado de petroleo.*vehicular',
                        r'gas licuado.*vehicular']
    }

    # -------- 1) intento por bloque “estimación” / viñetas ----------
    lines = [ln.strip() for ln in text_norm.splitlines() if ln.strip()]
    # intenta acotar a la sección de estimación, si existe
    start = next((i for i, ln in enumerate(lines) if 'estimacion de precios' in ln), None)
    end   = next((i for i, ln in enumerate(lines) if ln.startswith('santiago')), None)
    if start is not None and end is not None and end > start:
        seg = lines[start:end]
    else:
        seg = lines  # no encontrada la cabecera, trabaja con todo

    def set_if_match(label, ln):
        nonlocal out
        if out[label] is not None:
            return
        for pat in keys[label]:
            if re.search(pat, ln):
                val = _first_num(ln)
                if val is not None:
                    out[label] = val
                    return

    for ln in seg:
        # busca viñetas – o • o líneas corridas
        # extrae por etiqueta y toma el primer número de esa línea
        for k in out.keys():
            if k == "fecha":
                continue
            set_if_match(k, ln)

    # -------- 2) fallback global (por si algún número no salió por líneas) ----------
    if any(out[k] is None for k in out if k != "fecha"):
        patterns = {
            "gasolina_93": [
                rf'gasolina{WS}de{WS}93{WS}octanos.*?{NUM}{WS}{PESOLT}',
                rf'gasolina{WS}93.*?{NUM}{WS}{PESOLT}',
            ],
            "gasolina_97": [
                rf'gasolina{WS}de{WS}97{WS}octanos.*?{NUM}{WS}{PESOLT}',
                rf'gasolina{WS}97.*?{NUM}{WS}{PESOLT}',
            ],
            "diesel": [
                rf'dies[e]?l.*?{NUM}{WS}{PESOLT}',
            ],
            "kerosene": [
                rf'(?:kerosen[e]?|kerosen|parafina).*?{NUM}{WS}{PESOLT}',
            ],
            "glp": [
                rf'(?:\bglp\b|gas{WS}licuado{WS}de{WS}petroleo{WS}de{WS}uso{WS}vehicular|gas{WS}licuado.*vehicular).*?{NUM}{WS}{PESOLT}',
            ],
        }
        for k, pats in patterns.items():
            if k == "fecha" or out[k] is not None:
                continue
            for pat in pats:
                m = re.search(pat, text_norm)
                if m:
                    out[k] = float(m.group(1).replace(',', '.'))
                    break

    # -------- 3) No descartes si faltan algunos: guarda lo encontrado ----------
    have_any = any(out[k] is not None for k in out if k != "fecha")
    if not have_any:
        # nada de números; probablemente PDF imagen → descarta
        return None

    return out






# =========================
# Backfill y guardado
# =========================
def backfill_years(years: List[int]) -> pd.DataFrame:
    rows: List[Dict] = []
    fails: List[str] = []

    for y in years:
        links = list_pdfs_for_year(y)
        print(f"[backfill] {y}: {len(links)} PDFs totales (post-paginación)")
        for i, url in enumerate(links, 1):
            try:
                pdfp = download_pdf(url)
                row = parse_variations(pdfp)
                if row:
                    rows.append(row)
                else:
                    fails.append(f"{y}\t{i}\t{url}")
            except Exception as e:
                print("WARN parse:", y, i, url, "->", e)
                fails.append(f"{y}\t{i}\t{url}\tEXC:{e}")

    if fails:
        DATA_DIR.mkdir(exist_ok=True, parents=True)
        (DATA_DIR / "parse_fails.txt").write_text("\n".join(fails), encoding="utf-8")
        print(f"[backfill] Parse fails: {len(fails)} -> data/parse_fails.txt")

    if not rows:
        return pd.DataFrame(columns=["fecha","gasolina_93","gasolina_97","diesel","kerosene","glp"])

    df = (
        pd.DataFrame(rows)
        .drop_duplicates(subset=["fecha"])
        .sort_values("fecha")
        .reset_index(drop=True)
    )
    return df



def save_csv(df: pd.DataFrame) -> None:
    if df is None or df.empty:
        print("[backfill] DataFrame vacío: nada que guardar.")
        return

    for col in ["gasolina_93", "gasolina_97", "diesel", "kerosene", "glp"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df.to_csv(CSV_PATH, index=False, encoding="utf-8")
    print(f"[backfill] Guardado: {CSV_PATH} filas: {len(df)}")


# =========================
# main
# =========================
if __name__ == "__main__":
    try:
        YEARS = (2023, 2024, 2025)  # ajusta si quieres otros años
        print("[backfill] Iniciando backfill para años:", YEARS)

        # prueba simple
        r = requests.get(ARCHIVE_FIRST.format(year=YEARS[0]), headers=HEADERS, timeout=30)
        print("[backfill] Test status:", r.status_code, "len:", len(r.text))

        print("[backfill] Parseando PDFs y construyendo DataFrame...")
        t0 = time.time()
        df_hist = backfill_years(list(YEARS))
        print(f"[backfill] Filas parseadas: {len(df_hist)}  (t={time.time()-t0:.1f}s)")

        print("[backfill] Guardando CSV...")
        save_csv(df_hist)
        print("[backfill] DONE ✅ ->", CSV_PATH.resolve())

    except Exception as e:
        print("[backfill] ERROR:", e)
        raise
