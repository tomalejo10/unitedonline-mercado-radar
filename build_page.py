"""
Genera mercado_radar.html (autocontenido, listo para publicar como Artifact) a partir
de state.db + assets/. No pega a internet: solo lee lo que el bot ya cacheo.

Uso: python build_page.py [ruta_salida.html]
"""

import base64
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "state.db"
ASSETS_DIR = BASE_DIR / "assets"
TEMPLATE_PATH = BASE_DIR / "page_template.html"

CLASS_NAMES = {1: "Guerrero", 2: "Cazador", 3: "Paladín", 4: "Asesino", 5: "Clérigo",
               6: "Bardo", 7: "Mago", 8: "Druida", 9: "Bandido"}
RACE_NAMES = {1: "Humano", 2: "Elfo", 3: "Elfo oscuro", 4: "Gnomo", 5: "Enano", 6: "Orco"}


def img_hash(url):
    return hashlib.md5(url.encode()).hexdigest()


def find_asset(url):
    if not url:
        return None
    matches = list(ASSETS_DIR.glob(img_hash(url) + ".*"))
    return matches[0] if matches else None


def main():
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE_DIR / "mercado_radar.html"

    conn = sqlite3.connect(DB_PATH)
    listings = conn.execute(
        "SELECT listing_id, character_id, name, class_id, race_id, level, "
        "price_usdt_micros, online, created_at, faction FROM listings WHERE active = 1 "
        "ORDER BY price_usdt_micros ASC"
    ).fetchall()
    events = conn.execute(
        "SELECT ts, type, listing_id, name, detail FROM events ORDER BY id DESC LIMIT 200"
    ).fetchall()
    details_rows = conn.execute("SELECT listing_id, detail_json FROM listing_details").fetchall()
    generated_at = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
    conn.close()

    details = {lid: json.loads(dj) for lid, dj in details_rows}

    icons = {}  # hash -> data URI, deduplicado entre todos los personajes

    def icon_key(url):
        if not url:
            return None
        h = img_hash(url)
        if h not in icons:
            path = find_asset(url)
            if path is None:
                return None
            mime = "image/png" if path.suffix.lower() == ".png" else f"image/{path.suffix.lstrip('.')}"
            b64 = base64.b64encode(path.read_bytes()).decode()
            icons[h] = f"data:{mime};base64,{b64}"
        return h

    out_listings = []
    for lid, char_id, name, class_id, race_id, level, price_micros, online, created_at, faction in listings:
        item = {
            "listingId": lid,
            "characterId": char_id,
            "name": name,
            "classId": class_id,
            "raceId": race_id,
            "level": level,
            "priceUsdtMicros": price_micros,
            "online": bool(online),
            "createdAt": created_at,
            "faction": faction,
        }
        d = details.get(lid)
        if d:
            item["spriteIcon"] = icon_key(d.get("spriteUrl"))
            inv = []
            for it in (d.get("inventory") or []):
                inv.append({
                    "name": it.get("name"),
                    "amount": it.get("amount"),
                    "equipped": bool(it.get("equipped")),
                    "icon": icon_key(it.get("iconUrl")),
                })
            item["detail"] = {
                "gold": d.get("gold"),
                "bankGold": d.get("bank_gold"),
                "guild": d.get("guild"),
                "deaths": d.get("deaths"),
                "exp": d.get("exp"),
                "expNextLevel": d.get("exp_next_level"),
                "extraHp": d.get("extra_hp"),
                "extraHpCap": d.get("extra_hp_cap"),
                "freeSkillpoints": d.get("free_skillpoints"),
                "completedQuests": d.get("completed_quests"),
                "usersKilled": (d.get("statistics") or {}).get("users_killed"),
                "criminalsKilled": (d.get("statistics") or {}).get("criminals_killed"),
                "citizensKilled": (d.get("statistics") or {}).get("citizens_killed"),
                "inventory": inv,
            }
        out_listings.append(item)

    out_events = [
        {"ts": ts, "type": type_, "listingId": listing_id, "name": name, "detail": detail}
        for ts, type_, listing_id, name, detail in events
    ]

    from datetime import datetime, timezone
    payload = {
        "listings": out_listings,
        "events": out_events,
        "icons": icons,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "__SNAPSHOT_DATA__" in template
    out_html = template.replace("__SNAPSHOT_DATA__", json.dumps(payload, ensure_ascii=False))
    out_path.write_text(out_html, encoding="utf-8")

    # Hash del contenido real (sin generatedAt, que cambia siempre) -> permite detectar
    # si hubo cambios de verdad entre una corrida y otra, para no republicar al pedo.
    content_for_hash = {k: v for k, v in payload.items() if k != "generatedAt"}
    content_hash = hashlib.sha256(
        json.dumps(content_for_hash, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    (BASE_DIR / "last_content_hash.txt").write_text(content_hash, encoding="utf-8")

    print(f"OK -> {out_path} ({len(out_html):,} bytes, {len(out_listings)} listados, "
          f"{sum(1 for l in out_listings if 'detail' in l)} con detalle, {len(icons)} imagenes unicas)")
    print(f"content_hash={content_hash}")


if __name__ == "__main__":
    main()
