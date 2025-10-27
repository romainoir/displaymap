
"""
enrich_refuges.py
-----------------
Enrichit un GeoJSON de refuges (issu de l'API refuges.info) avec :
- "photos": liste d'URLs absolues des photos trouvées sur la page du point
- "thumbnail": une URL de miniature (par défaut la 1ère photo), utilisable pour l'icône sur la carte
- "amenities": dict standardisé (eau, bois, poele, latrines, cheminee, couvertures, couchage, feu)
- "amenities_text": courte phrase récapitulative pour affichage rapide
- "photos_at": timestamp ISO de la dernière collecte

Utilisation :
    python enrich_refuges.py input.json output.json [--concurrency 4] [--delay 0.4] [--max N]

Dépendances :
    pip install requests beautifulsoup4 tqdm

Notes :
- Le script respecte un petit délai entre requêtes (--delay) pour être courtois.
- On consolide aussi les informations "info_comp" du GeoJSON d'origine lorsqu'elles existent.
- La détection des photos suit la logique JS historique: chemins /photos_points/...-reduite.jpeg

Auteur: vous 🤗
"""
import argparse
import concurrent.futures
import datetime as dt
import json
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

BASE = "https://www.refuges.info"

PHOTO_PATTERNS = [
    re.compile(r"/photos_points/\d+-reduite\.jpeg", re.IGNORECASE),
    re.compile(r"/photos_points/\d+\.jpg", re.IGNORECASE),
    re.compile(r"/photos_points/\d+\.jpeg", re.IGNORECASE),
]

def normalize_abs(url: str) -> str:
    if not url:
        return url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("//"):
        return "https:" + url
    return urljoin(BASE, url)

def extract_photos_from_html(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        for pat in PHOTO_PATTERNS:
            if pat.search(src):
                urls.append(src)
                break
    # also check "data-src" (lazy loading)
    for img in soup.find_all("img"):
        src = (img.get("data-src") or "").strip()
        for pat in PHOTO_PATTERNS:
            if pat.search(src):
                urls.append(src)
                break
    # uniq + absolutize
    uniq = []
    seen = set()
    for u in urls:
        absu = normalize_abs(u)
        if absu not in seen:
            seen.add(absu)
            uniq.append(absu)
    return uniq

def summarize_amenities(props: dict) -> tuple[dict, str]:
    # info_comp structure (quand présente) : booleens "1"/"0"
    info = props.get("info_comp") or {}
    # Certaines API renvoient du JSON sous forme de chaîne
    if isinstance(info, str) and info.startswith("{"):
        try:
            info = json.loads(info)
        except Exception:
            info = {}
    getv = lambda key: str(((info.get(key) or {}) if isinstance(info.get(key), dict) else {"valeur": info.get(key)}).get("valeur") or "").strip()

    # Consolide couchage/feu à partir de champs voisins quand disponibles
    amenities = {
        "eau": getv("eau") == "1",
        "bois": getv("bois") == "1",
        "poele": getv("poele") == "1",
        "latrines": getv("latrines") == "1",
        "cheminee": getv("cheminee") == "1",
        "couvertures": getv("couvertures") == "1",
    }
    # Couchage: places > 0 ou couvertures
    places = props.get("places") or {}
    if isinstance(places, str) and places.startswith("{"):
        try:
            places = json.loads(places)
        except Exception:
            places = {}
    try:
        cap = int(str((places.get("valeur") if isinstance(places, dict) else places) or "0"))
    except Exception:
        cap = 0
    amenities["couchage"] = cap > 0 or amenities["couvertures"]

    # Feu: poêle ou cheminée
    amenities["feu"] = amenities["poele"] or amenities["cheminee"]

    tags = []
    if amenities["couchage"]:
        tags.append("🛏️ couchage")
    if amenities["feu"]:
        tags.append("🔥 feu")
    if amenities["eau"]:
        tags.append("💧 eau")
    if amenities["latrines"]:
        tags.append("🚽 latrines")

    text = " · ".join(tags) if tags else "—"
    return amenities, text

def fetch_point_html(url: str, session: requests.Session, retries=3, backoff=0.8, timeout=15):
    last_err = None
    for i in range(retries):
        try:
            r = session.get(url, timeout=timeout, headers={"User-Agent": "refuges-enricher/1.0"})
            if r.ok:
                return r.text
            last_err = RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:
            last_err = e
        time.sleep(backoff * (2**i))
    raise last_err or RuntimeError("unknown error")

def process_feature(feat: dict, delay: float, session: requests.Session):
    props = feat.get("properties") or {}
    page_url = props.get("lien") or props.get("url") or ""
    if not page_url:
        return feat  # nothing to do

    try:
        html = fetch_point_html(page_url, session=session)
        photos = extract_photos_from_html(html)
    except Exception:
        photos = []

    thumb = photos[0] if photos else None
    amenities, amen_text = summarize_amenities(props)

    props["photos"] = photos
    if thumb:
        props["thumbnail"] = thumb
    props["amenities"] = amenities
    props["amenities_text"] = amen_text
    props["photos_at"] = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"

    feat["properties"] = props
    if delay:
        time.sleep(delay)
    return feat

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="GeoJSON d'entrée (refuges.info)")
    ap.add_argument("output", help="GeoJSON de sortie enrichi")
    ap.add_argument("--concurrency", type=int, default=4, help="Nombre de requêtes parallèles (par défaut 4)")
    ap.add_argument("--delay", type=float, default=0.4, help="Délai (s) entre requêtes pour courtoisie")
    ap.add_argument("--max", type=int, default=0, help="Limiter au N premiers points (debug)")
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    feats = data.get("features") or []
    if args.max and args.max > 0:
        feats = feats[: args.max]

    out_feats = []
    sess = requests.Session()

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        futs = []
        for feat in feats:
            futs.append(ex.submit(process_feature, feat, args.delay, sess))

        for fut in tqdm(concurrent.futures.as_completed(futs), total=len(futs), desc="Enrichissement"):
            out_feats.append(fut.result())

    # On conserve les features non traitées (si --max) + celles traitées
    if args.max and args.max > 0:
        data["features"][: len(out_feats)] = out_feats
    else:
        data["features"] = out_feats

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"), indent=2)

    print(f"OK -> {args.output}")

if __name__ == "__main__":
    main()
