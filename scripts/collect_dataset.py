"""
Сбор датасета поверхностей (стены / потолки / полы) через Pexels API.

Использование:
    export PEXELS_API_KEY="ваш_ключ"
    python collect_dataset.py --per-class 80 --out ./dataset

Ключ получить бесплатно тут: https://www.pexels.com/api/
"""

import argparse
import hashlib
import io
import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

import requests
from PIL import Image

PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(ENV_PATH)


CLASSES = {
    "walls": {
        "bare_masonry": ["bare masonry wall interior", "exposed masonry wall unfinished"],
        "bare_concrete": ["bare concrete wall interior", "raw concrete wall texture"],
        "exposed_drywall": ["exposed drywall wall unfinished", "drywall seams unfinished wall"],
        "unfinished_plaster_or_putty": ["unfinished plaster wall construction", "putty wall texture construction"],
        "painted": ["painted wall interior texture", "flat painted wall room plain"],
        "wallpaper": ["wallpaper wall interior texture", "patterned wallpaper wall room"],
        "tile": ["wall tile texture bathroom", "kitchen wall tile interior"],
        "textured_decorative_finish": ["decorative textured wall finish", "venetian plaster wall texture"],
        "wall_paneling": ["wood paneling wall interior", "wooden wall panel room"],
        "decorative_stone_or_brick": ["decorative stone wall interior", "brick accent wall interior"],
    },
    "floors": {
        "bare_concrete_or_screed": ["bare concrete floor screed", "raw concrete floor construction"],
        "bare_wood_subfloor": ["wood subfloor construction", "plywood subfloor interior"],
        "tile_or_stone": ["tile floor texture interior", "stone floor texture interior"],
        "wood_laminate_or_parquet": ["wood floor texture interior", "parquet flooring interior"],
        "vinyl_or_linoleum": ["vinyl flooring texture interior", "linoleum floor interior"],
        "carpet": ["carpet floor texture interior", "carpet flooring room"],
        "seamless_resin_floor": ["epoxy resin floor interior", "seamless resin flooring"],
    },
    "ceilings": {
        "bare_concrete": ["exposed concrete ceiling interior", "bare concrete ceiling"],
        "exposed_drywall": ["unfinished drywall ceiling", "drywall ceiling construction"],
        "unfinished_plaster_or_putty": ["unfinished plaster ceiling construction", "putty ceiling construction"],
        "painted": ["painted ceiling white interior", "flat ceiling room plain"],
        "wallpaper": ["ceiling wallpaper pattern interior", "decorative ceiling wallpaper"],
        "textured_decorative_finish": ["decorative textured ceiling", "stucco ceiling texture"],
        "stretch_ceiling": ["stretch ceiling interior", "PVC stretch ceiling glossy"],
        "suspended_tile_ceiling": ["suspended ceiling tile office", "drop ceiling tile interior"],
        "panel_ceiling": ["wood panel ceiling interior", "ceiling panels interior"],
    },
}

HEADERS_TEMPLATE = {"Authorization": ""}


def get_headers(api_key: str) -> dict:
    return {"Authorization": api_key}


def search_photos(query: str, api_key: str, per_page: int, page: int) -> list:
    params = {"query": query, "per_page": per_page, "page": page, "orientation": "landscape"}
    resp = requests.get(PEXELS_SEARCH_URL, headers=get_headers(api_key), params=params, timeout=30)
    if resp.status_code == 429:
        print("  [!] Rate limit reached, pause 60 s...")
        time.sleep(60)
        resp = requests.get(PEXELS_SEARCH_URL, headers=get_headers(api_key), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("photos", [])


def download_image(url: str, dest_path: Path) -> bool:
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
        img.verify()
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        img.save(dest_path, format="JPEG", quality=90)
        return True
    except Exception as e:
        print(f"    [x] Filed to download {url}: {e}")
        return False


def collect_class(surface: str, class_name: str, queries: list, api_key: str,
                   target_count: int, out_dir: Path, min_size: int = 400):
    class_dir = out_dir / surface / class_name
    class_dir.mkdir(parents=True, exist_ok=True)

    existing = len(list(class_dir.glob("*.jpg")))
    if existing >= target_count:
        print(f"[=] {surface}/{class_name}: already exist {existing}, pass")
        return

    seen_ids = set()
    collected = existing
    print(f"[>] {surface}/{class_name}: target {target_count}, already exist {existing}")

    for query in queries:
        if collected >= target_count:
            break
        page = 1
        while collected < target_count:
            photos = search_photos(query, api_key, per_page=80, page=page)
            if not photos:
                break
            for photo in photos:
                if collected >= target_count:
                    break
                photo_id = photo["id"]
                if photo_id in seen_ids:
                    continue
                seen_ids.add(photo_id)

                if photo["width"] < min_size or photo["height"] < min_size:
                    continue

                img_url = photo["src"]["large"]
                fname = hashlib.md5(img_url.encode()).hexdigest()[:12] + ".jpg"
                dest = class_dir / fname
                if dest.exists():
                    continue

                if download_image(img_url, dest):
                    collected += 1
                    print(f"    [{collected}/{target_count}] {fname}")
                time.sleep(0.15)

            page += 1
            time.sleep(0.5)

    print(f"[✓] {surface}/{class_name}: total {collected} images\n")


def main():
    parser = argparse.ArgumentParser(description="Collecting a dataset of surfaces from Pexels API")
    parser.add_argument("--per-class", type=int, default=80, help="Number of photos per class")
    parser.add_argument("--out", type=str, default="./data/dataset", help="Folder to save collected dataset")
    parser.add_argument("--api-key", type=str, default=os.environ.get("PEXELS_API_KEY"),
                         help="Pexels API key (PEXELS_API_KEY)")
    parser.add_argument("--surfaces", type=str, default="walls,floors,ceilings",
                         help="how many surfaces to collect (walls, floors, ceilings)")
    args = parser.parse_args()

    if not args.api_key:
        print("Error: API key not specified. Pass --api-key or set PEXELS_API_KEY.")
        sys.exit(1)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    surfaces_to_run = [s.strip() for s in args.surfaces.split(",")]

    for surface in surfaces_to_run:
        if surface not in CLASSES:
            print(f"[!] Unknown surface: {surface}, pass")
            continue
        for class_name, queries in CLASSES[surface].items():
            collect_class(surface, class_name, queries, args.api_key, args.per_class, out_dir)

    print("Done! dataset collected in:", out_dir.resolve())


if __name__ == "__main__":
    main()
