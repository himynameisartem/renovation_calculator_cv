"""
Сбор датасета поверхностей (стены / потолки / полы) через Pexels API.

Использование:
    export PEXELS_API_KEY="ваш_ключ"
    python collect_dataset.py --per-class 80 --out ./dataset

Ключ получить бесплатно тут: https://www.pexels.com/api/
"""

import argparse
import json
import csv
import hashlib
import io
import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

import requests
from PIL import Image, ImageOps

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


def image_digest(image):
    image = ImageOps.exif_transpose(image).convert("RGB")
    header = f"{image.width}x{image.height}:".encode()
    return hashlib.sha256(header + image.tobytes()).hexdigest()


def build_index(out_dir, surfaces):
    names, digests, ids = set(), set(), set()
    manifest_path = out_dir / "collection_manifest.jsonl"
    if manifest_path.exists():
        for line in manifest_path.read_text().splitlines():
            record = json.loads(line)
            if record.get("surface") in surfaces:
                ids.add(record["photo_id"])
    duplicates, first = [], {}
    for surface in surfaces:
        for path in sorted((out_dir / surface).rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            names.add(path.name)
            try:
                with Image.open(path) as image:
                    digest = image_digest(image)
                digests.add(digest)
                if digest in first:
                    duplicates.append({"original": str(first[digest]), "duplicate": str(path)})
                else:
                    first[digest] = path
            except (OSError, ValueError):
                print("Unreadable existing image:", path)
    report = out_dir / "existing_exact_duplicates.csv"
    with report.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["original", "duplicate"])
        writer.writeheader(); writer.writerows(duplicates)
    print("Existing duplicate copies:", len(duplicates), "Report:", report)
    return names, digests, ids


def collect_class(surface, class_name, queries, api_key, target_count, out_dir,
                  index, min_size=400):
    class_dir = out_dir / surface / class_name
    class_dir.mkdir(parents=True, exist_ok=True)
    collected = len(list(class_dir.glob("*.jpg")))
    names, digests, ids = index
    print(f"{surface}/{class_name}: existing={collected}, target={target_count}")
    attempted = set()
    for query in queries:
        page = 1
        while collected < target_count:
            photos = search_photos(query, api_key, per_page=80, page=page)
            if not photos:
                break
            # Stop if the provider repeats a page.
            fresh = [photo for photo in photos if photo["id"] not in attempted]
            if not fresh:
                break
            for photo in fresh:
                photo_id = photo["id"]
                attempted.add(photo_id)
                if collected >= target_count:
                    break
                if photo_id in ids or min(photo["width"], photo["height"]) < min_size:
                    continue
                url = photo["src"]["large"]
                fname = hashlib.md5(url.encode()).hexdigest()[:12] + ".jpg"
                if fname in names:
                    ids.add(photo_id)
                    continue
                try:
                    response = requests.get(url, timeout=30)
                    response.raise_for_status()
                    with Image.open(io.BytesIO(response.content)) as source:
                        image = ImageOps.exif_transpose(source).convert("RGB")
                    # Compare the exact saved JPEG pixels, matching existing files.
                    encoded = io.BytesIO()
                    image.save(encoded, format="JPEG", quality=90)
                    payload = encoded.getvalue()
                    with Image.open(io.BytesIO(payload)) as saved:
                        digest = image_digest(saved)
                    ids.add(photo_id)
                    if digest in digests:
                        continue
                    destination = class_dir / fname
                    destination.write_bytes(payload)
                    names.add(fname); digests.add(digest)
                    record = dict(surface=surface, label=class_name, photo_id=photo_id,
                                  query=query, source_url=photo.get("url"), image_url=url,
                                  path=str(destination.resolve()), pixel_sha256=digest)
                    with (out_dir / "collection_manifest.jsonl").open("a") as file:
                        file.write(json.dumps(record, ensure_ascii=False) + "\n")
                    collected += 1
                    print(f"  {collected}/{target_count}: {fname}")
                except (requests.RequestException, OSError, ValueError) as error:
                    print("Download failed:", photo_id, type(error).__name__)
                time.sleep(.15)
            page += 1
            time.sleep(.5)
    print(f"{surface}/{class_name}: total={collected}")


def main():
    parser = argparse.ArgumentParser(description="Collecting a dataset of surfaces from Pexels API")
    parser.add_argument("--per-class", type=int, default=320, help="Target total photos per class, including existing files")
    parser.add_argument("--out", type=str, default=str(Path(__file__).resolve().parents[1] / "data/dataset"), help="Folder to save collected dataset")
    parser.add_argument("--api-key", type=str, default=os.environ.get("PEXELS_API_KEY"),
                         help="Pexels API key (PEXELS_API_KEY)")
    parser.add_argument("--surfaces", type=str, default="walls",
                         help="how many surfaces to collect (walls, floors, ceilings)")
    parser.add_argument("--audit-only", action="store_true", help="Report existing exact duplicates without downloading")
    args = parser.parse_args()

    if not args.api_key and not args.audit_only:
        print("Error: API key not specified. Pass --api-key or set PEXELS_API_KEY.")
        sys.exit(1)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    surfaces_to_run = [s.strip() for s in args.surfaces.split(",")]

    if any(s not in CLASSES for s in surfaces_to_run):
        parser.error("Unknown surface")
    if args.per_class <= 0:
        parser.error("--per-class must be positive")
    index = build_index(out_dir, surfaces_to_run)
    if args.audit_only:
        return

    for surface in surfaces_to_run:
        if surface not in CLASSES:
            print(f"[!] Unknown surface: {surface}, pass")
            continue
        for class_name, queries in CLASSES[surface].items():
            collect_class(surface, class_name, queries, args.api_key, args.per_class, out_dir, index)

    print("Done! dataset collected in:", out_dir.resolve())


if __name__ == "__main__":
    main()
