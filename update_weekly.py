# update_weekly.py  — FuelWatch CL
# Descarga el último PDF de ENAP, parsea variaciones ($/lt) y actualiza data/variaciones_semana.csv

import re
import unicodedata
from pathlib import Path

import pandas as pd
import pdfplumber
import requests
from bs4 import BeautifulSoup

# --- rutas robustas (siempre relativas a este archivo) ---
BASE_DIR = Path(__file__).resolve().parent
DATA = BASE_DIR / "data"
DATA.mkdir(exist_ok=True)

CSV = DATA / "variaciones_semana.csv"
TMP = DATA / "latest.pdf"
BASE = "https://www.enap.cl"

# ----------------- utilidades -----------------
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.lower()

def get_latest_pdf_url() -> str:
    year = pd.Timestamp.today().year
    url = f"{BASE}/archivos/8/informe-semanal-de-precios?year={year}"
    html = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (FuelWatch-CL)"}).text
    a = BeautifulSoup(html, "html.parser").select_one("a[href*='/files/get/']")
    href = a["href"]
    return href if href.startswith("http") else BASE + href

def _nums_in(s: str):
    # capta +12,3  -18,3  0,0  etc.
    return [float(x.replace(",", ".")) for x in re.findall(r"[+-]?\d+,\d", s)]

def parse_variations_v3(pdf_path: Path):
    with pdfplumber.open(pdf_path) as pdf:
        raw = "\n".join(p.extract_text() or "" for p in pdf.pages)
    tnorm = _norm(raw)

    # fecha "Santiago, 22 de octubre de 2025"
    MES = {
        "enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,
        "julio":7,"agosto":8,"septiembre":9,"octubre":10,"noviembre":11,"diciembre":12
    }
    mdate = re.search(r"santiago,\s+(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", tnorm)
    fecha = None
    if mdate:
        import datetime as _dt
        d, mon, y = int(mdate.group(1)), MES.get(mdate.group(2)), int(mdate.group(3))
        if mon:
            fecha = _dt.date(y, mon, d).isoformat()

    out = {"fecha": fecha, "gasolina_93": None, "gasolina_97": None, "diesel": None, "kerosene": None, "glp": None}

    # barrido por líneas
    for line in raw.splitlines():
        ln = _norm(line)
        ns = _nums_in(ln)

        # 93 y 97 en la misma línea (caso más común)
        if (" 93 " in f" {ln} " or " 93" in ln) and (" 97 " in f" {ln} " or " 97" in ln) and len(ns) >= 2:
            out["gasolina_93"], out["gasolina_97"] = ns[0], ns[1]
            continue

        # individuales
        if (" diesel" in f" {ln}") or (" diésel" in ln):
            if ns: out["diesel"] = ns[0];  continue
        if ("kerosene" in ln) or ("parafina" in ln):
            if ns: out["kerosene"] = ns[0];  continue
        if (" glp " in f" {ln} ") or ("glp de uso vehicular" in ln) or ("gas licuado" in ln):
            if ns: out["glp"] = ns[0];  continue
        if " 93 " in f" {ln} " and out["gasolina_93"] is None:
            if ns: out["gasolina_93"] = ns[0]
        if " 97 " in f" {ln} " and out["gasolina_97"] is None:
            if ns: out["gasolina_97"] = ns[0]

    # fallback: si no encontró número, fuerza 0.0 (seguro para dashboard)
    for k, keyfrag in [("gasolina_93"," 93 "),("gasolina_97"," 97 "),("diesel","diesel"),("kerosene","kerosene"),("glp","glp")]:
        if out[k] is None and keyfrag in tnorm and "0,0" in tnorm:
            out[k] = 0.0
        if out[k] is None:
            out[k] = 0.0

    return out

# ----------------- main -----------------
def main():
    latest = get_latest_pdf_url()
    pdf_bytes = requests.get(latest, headers={"User-Agent":"Mozilla/5.0 (FuelWatch-CL)"}).content
    TMP.write_bytes(pdf_bytes)

    row = parse_variations_v3(TMP)

    # normalización extra
    for k in ["gasolina_93","gasolina_97","diesel","kerosene","glp"]:
        row[k] = 0.0 if row.get(k) is None else float(row[k])

    # crear/cargar CSV y agregar si es fecha nueva
    if CSV.exists():
        df = pd.read_csv(CSV)
    else:
        df = pd.DataFrame(columns=["fecha","gasolina_93","gasolina_97","diesel","kerosene","glp"])

    if row["fecha"] in set(df["fecha"].astype(str)):
        print("Semana ya registrada:", row["fecha"])
        return

    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df = df.drop_duplicates(subset=["fecha"]).sort_values("fecha")
    df.to_csv(CSV, index=False, encoding="utf-8")
    print("Agregada semana:", row["fecha"])

if __name__ == "__main__":
    main()
