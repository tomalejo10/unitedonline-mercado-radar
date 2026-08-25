"""
Bot de trackeo del mercado de United Online.

Cada corrida:
1. Trae el listado completo de https://unitedonline.com.ar/mercado/ (via Playwright,
   porque la API tiene Cloudflare delante y bloquea requests directos).
2. Compara contra el snapshot anterior guardado en state.db:
   - nuevos listados
   - cambios de precio
   - listados que desaparecieron -> se busca en la blockchain de BSC (RPC publico,
     sin API key) una transferencia de USDT a la wallet del mercado que coincida
     en monto -> si aparece, se marca como VENDIDO; si no, RETIRADO/VENCIDO.
3. Deja todo en state.db, agrega lineas a events.log y regenera dashboard.html.

Pensado para correr cada 5 minutos via Task Scheduler de Windows.
"""

import hashlib
import json
import os
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "state.db"
LOG_PATH = BASE_DIR / "events.log"
DASHBOARD_PATH = BASE_DIR / "dashboard.html"
ASSETS_DIR = BASE_DIR / "assets"
ASSETS_DIR.mkdir(exist_ok=True)

MARKET_PAGE_URL = "https://unitedonline.com.ar/mercado/"
MARKET_API_URL = "https://api.unitedonline.com.ar/api/market/listings?sort=newest&page={page}"
DETAIL_API_URL = "https://api.unitedonline.com.ar/api/market/listings/{listing_id}?lang=es"

WALLET = "0xefD350d4655eee07d9B3BCF94B0514f00d02e1E8"
USDT_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"  # BSC-USD, 18 decimales
TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Key gratuita de https://ankr.com/rpc — los nodos publicos sin key limitan mucho
# eth_getLogs (confirmado: fallan incluso con rangos de 10 bloques). Sin esto el
# chequeo de ventas queda pendiente hasta que algun RPC publico responda.
# Se lee de una variable de entorno (GitHub secret en CI) — nunca hardcodeada,
# porque este repo es publico.
ANKR_API_KEY = os.environ.get("ANKR_API_KEY", "")

RPC_ENDPOINTS = [
    "https://bsc-dataseed.binance.org",
    "https://bsc-dataseed1.defibit.io",
    "https://bsc-dataseed1.ninicoin.io",
]
if ANKR_API_KEY:
    RPC_ENDPOINTS.insert(0, f"https://rpc.ankr.com/bsc/{ANKR_API_KEY}")

PRICE_MATCH_TOLERANCE_USDT = 0.01
LOOKBACK_BLOCKS_SAFETY = 200  # ~10 min de margen al marcar un listado como "desaparecido"
MAX_WAIT_MINUTES = 45  # ventana de pago (30 min) + margen antes de dar por "retirado_o_vencido"

CLASS_NAMES = {1: "Guerrero", 2: "Cazador", 3: "Paladín", 4: "Asesino", 5: "Clérigo",
               6: "Bardo", 7: "Mago", 8: "Druida", 9: "Bandido"}
RACE_NAMES = {1: "Humano", 2: "Elfo", 3: "Elfo oscuro", 4: "Gnomo", 5: "Enano", 6: "Orco"}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def log_line(text):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{now_iso()}] {text}\n")


# --------------------------------------------------------------------------
# DB
# --------------------------------------------------------------------------

def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            listing_id INTEGER PRIMARY KEY,
            character_id INTEGER,
            name TEXT,
            class_id INTEGER,
            race_id INTEGER,
            level INTEGER,
            price_usdt_micros INTEGER,
            online INTEGER,
            created_at TEXT,
            active INTEGER DEFAULT 1,
            first_seen TEXT,
            last_seen TEXT,
            faction INTEGER
        )
    """)
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
    if "faction" not in existing_cols:
        conn.execute("ALTER TABLE listings ADD COLUMN faction INTEGER")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            type TEXT,
            listing_id INTEGER,
            name TEXT,
            detail TEXT
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_sales (
            listing_id INTEGER PRIMARY KEY,
            name TEXT,
            price_usdt_micros INTEGER,
            disappeared_at TEXT,
            from_block INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listing_details (
            listing_id INTEGER PRIMARY KEY,
            detail_json TEXT,
            fetched_at TEXT
        )
    """)
    conn.commit()


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def add_event(conn, type_, listing_id, name, detail):
    conn.execute("INSERT INTO events (ts, type, listing_id, name, detail) VALUES (?, ?, ?, ?, ?)",
                 (now_iso(), type_, listing_id, name, detail))
    log_line(f"{type_} | {name} (listing {listing_id}) | {detail}")


# --------------------------------------------------------------------------
# Scraping del mercado (via navegador headless, esquiva Cloudflare)
# --------------------------------------------------------------------------

def fetch_page(page, page_num, retries=5):
    url = MARKET_API_URL.format(page=page_num)
    for attempt in range(1, retries + 1):
        try:
            return page.evaluate("url => fetch(url).then(r => r.json())", url)
        except Exception:  # noqa: BLE001
            if attempt == retries:
                raise
            time.sleep(min(10, 2 * attempt))


def fetch_all_listings(conn):
    listings = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(MARKET_PAGE_URL, wait_until="networkidle", timeout=30000)

        first = fetch_page(page, 1)
        total = first.get("total", 0)
        page_size = first.get("pageSize", 24)
        total_pages = max(1, -(-total // page_size))  # ceil

        def consume(payload):
            for item in payload.get("listings", []):
                listings[item["listingId"]] = item

        consume(first)
        for p_num in range(2, total_pages + 1):
            time.sleep(0.4)
            consume(fetch_page(page, p_num))

        ensure_details_cached(conn, page, listings.keys())

        browser.close()
    return listings


def fetch_detail(page, listing_id, retries=3):
    url = DETAIL_API_URL.format(listing_id=listing_id)
    for attempt in range(1, retries + 1):
        try:
            return page.evaluate("url => fetch(url).then(r => r.json())", url)
        except Exception:  # noqa: BLE001
            if attempt == retries:
                raise
            time.sleep(1.5 * attempt)


def download_image(url):
    """Descarga una imagen a ASSETS_DIR (una sola vez, cacheada por hash de la URL)."""
    if not url:
        return None
    ext = url.rsplit(".", 1)[-1].split("?")[0] if "." in url.rsplit("/", 1)[-1] else "png"
    local_path = ASSETS_DIR / f"{hashlib.md5(url.encode()).hexdigest()}.{ext}"
    if local_path.exists():
        return local_path
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            local_path.write_bytes(resp.read())
        return local_path
    except Exception as e:  # noqa: BLE001
        log_line(f"WARN no se pudo descargar imagen {url}: {e}")
        return None


def ensure_details_cached(conn, page, listing_ids):
    """Trae y cachea (para siempre, en state.db + assets/) el detalle completo de cada
    listing nuevo: oro, clan, facción, inventario, sprite, íconos de items, etc. Como el
    personaje queda en custodia mientras está listado, este detalle no cambia -> se
    fetchea una sola vez por listing, no en cada corrida."""
    to_fetch = [
        lid for lid in listing_ids
        if not conn.execute("SELECT 1 FROM listing_details WHERE listing_id = ?", (lid,)).fetchone()
    ]
    for lid in to_fetch:
        try:
            data = fetch_detail(page, lid)
        except Exception as e:  # noqa: BLE001
            log_line(f"WARN no se pudo traer el detalle del listing {lid}: {e}")
            continue
        char = data.get("character") or {}
        conn.execute(
            "INSERT OR REPLACE INTO listing_details (listing_id, detail_json, fetched_at) VALUES (?, ?, ?)",
            (lid, json.dumps(char, ensure_ascii=False), now_iso()),
        )
        download_image(char.get("spriteUrl"))
        for item in (char.get("inventory") or []) + (char.get("vault") or []):
            download_image(item.get("iconUrl"))
        conn.commit()
        time.sleep(0.2)


# --------------------------------------------------------------------------
# Blockchain (RPC publico de BSC, sin API key)
# --------------------------------------------------------------------------

def rpc_call(method, params, retries=3):
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    last_err = None
    for attempt in range(1, retries + 1):
        for endpoint in RPC_ENDPOINTS:
            try:
                req = urllib.request.Request(
                    endpoint, data=payload, headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = json.loads(resp.read())
                    if "error" in body:
                        last_err = body["error"]
                        continue
                    return body["result"]
            except Exception as e:  # noqa: BLE001 - probamos el siguiente endpoint igual
                last_err = e
                continue
        if attempt < retries:
            time.sleep(2 * attempt)
    raise RuntimeError(f"Todos los RPC fallaron: {last_err}")


def get_latest_block():
    return int(rpc_call("eth_blockNumber", []), 16)


GETLOGS_MAX_RANGE = 900  # el free tier de Ankr corta eth_getLogs en rangos >1000 bloques


def get_incoming_usdt_transfers(from_block, to_block):
    """Devuelve lista de {tx_hash, from, amount_usdt, block_number} de transferencias
    de USDT recibidas por WALLET entre from_block y to_block (inclusive). Trocea la
    consulta en bloques de GETLOGS_MAX_RANGE porque el RPC rechaza rangos muy grandes."""
    topic_to = "0x" + "0" * 24 + WALLET[2:].lower()
    transfers = []
    chunk_start = from_block
    while chunk_start <= to_block:
        chunk_end = min(chunk_start + GETLOGS_MAX_RANGE, to_block)
        logs = rpc_call("eth_getLogs", [{
            "fromBlock": hex(chunk_start),
            "toBlock": hex(chunk_end),
            "address": USDT_CONTRACT,
            "topics": [TRANSFER_TOPIC0, None, topic_to],
        }])
        for entry in logs:
            amount = int(entry["data"], 16) / 1e18
            from_addr = "0x" + entry["topics"][1][-40:]
            transfers.append({
                "tx_hash": entry["transactionHash"],
                "from": from_addr,
                "amount_usdt": amount,
                "block_number": int(entry["blockNumber"], 16),
            })
        chunk_start = chunk_end + 1
    return transfers


# --------------------------------------------------------------------------
# Logica principal
# --------------------------------------------------------------------------

def run():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    current = fetch_all_listings(conn)
    prev_rows = conn.execute(
        "SELECT listing_id, character_id, name, class_id, race_id, level, "
        "price_usdt_micros, online, created_at FROM listings WHERE active = 1"
    ).fetchall()
    prev = {r[0]: r for r in prev_rows}

    is_first_run = len(prev) == 0 and get_meta(conn, "seeded") is None

    new_ids = set(current) - set(prev)
    gone_ids = set(prev) - set(current)
    common_ids = set(current) & set(prev)

    ts = now_iso()

    if is_first_run:
        for lid, item in current.items():
            conn.execute(
                "INSERT INTO listings (listing_id, character_id, name, class_id, race_id, "
                "level, price_usdt_micros, online, created_at, active, first_seen, last_seen, faction) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (lid, item["characterId"], item["name"], item["classId"], item["raceId"],
                 item["level"], item["priceUsdtMicros"], int(item["online"]),
                 item["createdAt"], ts, ts, item["faction"]),
            )
        set_meta(conn, "seeded", "1")
        set_meta(conn, "last_checked_block", get_latest_block())
        add_event(conn, "seed_inicial", None, "-", f"{len(current)} personajes cargados")
        conn.commit()
        conn.close()
        write_dashboard()
        return

    # nuevos
    for lid in new_ids:
        item = current[lid]
        conn.execute(
            "INSERT INTO listings (listing_id, character_id, name, class_id, race_id, "
            "level, price_usdt_micros, online, created_at, active, first_seen, last_seen, faction) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (lid, item["characterId"], item["name"], item["classId"], item["raceId"],
             item["level"], item["priceUsdtMicros"], int(item["online"]),
             item["createdAt"], ts, ts, item["faction"]),
        )
        price = item["priceUsdtMicros"] / 1e6
        clase = CLASS_NAMES.get(item["classId"], item["classId"])
        raza = RACE_NAMES.get(item["raceId"], item["raceId"])
        add_event(conn, "nuevo_listado", lid, item["name"],
                  f"{clase} · {raza} · Nivel {item['level']} · {price:g} USDT")

    # precio cambiado / sigue online
    for lid in common_ids:
        item = current[lid]
        prev_row = prev[lid]
        prev_price = prev_row[6]
        new_price = item["priceUsdtMicros"]
        if new_price != prev_price:
            old_usdt = prev_price / 1e6
            new_usdt = new_price / 1e6
            direction = "bajó" if new_price < prev_price else "subió"
            add_event(conn, "cambio_precio", lid, item["name"],
                      f"precio {direction}: {old_usdt:g} -> {new_usdt:g} USDT")
        conn.execute(
            "UPDATE listings SET price_usdt_micros = ?, online = ?, last_seen = ?, faction = ? "
            "WHERE listing_id = ?",
            (new_price, int(item["online"]), ts, item["faction"], lid),
        )

    # desaparecidos -> quedan pendientes de confirmar contra la blockchain
    # (no se decide vendido/retirado en el momento: se resuelve en resolve_pending_sales,
    # con reintentos, para no dar un veredicto falso si el RPC falla o tarda)
    if gone_ids:
        try:
            safety_block = get_latest_block() - LOOKBACK_BLOCKS_SAFETY
        except Exception as e:  # noqa: BLE001
            safety_block = None
            log_line(f"ERROR obteniendo bloque actual (los pendientes usan bloque 0 como fallback): {e}")
        for lid in gone_ids:
            row = prev[lid]
            conn.execute(
                "INSERT OR IGNORE INTO pending_sales (listing_id, name, price_usdt_micros, "
                "disappeared_at, from_block) VALUES (?, ?, ?, ?, ?)",
                (lid, row[2], row[6], ts, max(0, safety_block) if safety_block else 0),
            )
            conn.execute("UPDATE listings SET active = 0, last_seen = ? WHERE listing_id = ?", (ts, lid))

    resolve_pending_sales(conn)

    conn.commit()
    conn.close()
    write_dashboard()


def resolve_pending_sales(conn):
    pending = conn.execute(
        "SELECT listing_id, name, price_usdt_micros, disappeared_at, from_block FROM pending_sales"
    ).fetchall()
    if not pending:
        return

    try:
        latest_block = get_latest_block()
        from_block = min(row[4] for row in pending)
        transfers = get_incoming_usdt_transfers(from_block, latest_block)
    except Exception as e:  # noqa: BLE001
        log_line(f"ERROR consultando blockchain (se reintenta la próxima corrida): {e}")
        return

    now = datetime.now(timezone.utc)
    used_tx = set()
    # los mas viejos primero, para priorizarlos si dos precios coinciden
    for lid, name, price_micros, disappeared_at, _from_block in sorted(pending, key=lambda r: r[3]):
        price_usdt = price_micros / 1e6
        match = None
        for t in transfers:
            if t["tx_hash"] in used_tx:
                continue
            if abs(t["amount_usdt"] - price_usdt) <= PRICE_MATCH_TOLERANCE_USDT:
                match = t
                break

        if match:
            used_tx.add(match["tx_hash"])
            add_event(conn, "vendido", lid, name,
                      f"{price_usdt:g} USDT | tx {match['tx_hash']} | comprador {match['from']}")
            conn.execute("DELETE FROM pending_sales WHERE listing_id = ?", (lid,))
            continue

        age_min = (now - datetime.fromisoformat(disappeared_at)).total_seconds() / 60
        if age_min > MAX_WAIT_MINUTES:
            add_event(conn, "retirado_o_vencido", lid, name,
                      f"{price_usdt:g} USDT | sin transferencia coincidente tras {int(age_min)} min")
            conn.execute("DELETE FROM pending_sales WHERE listing_id = ?", (lid,))
        # si no, sigue pendiente: se reintenta en la proxima corrida


# --------------------------------------------------------------------------
# Dashboard estatico
# --------------------------------------------------------------------------

def write_dashboard():
    conn = sqlite3.connect(DB_PATH)
    listings = conn.execute(
        "SELECT name, class_id, race_id, level, price_usdt_micros, online, first_seen "
        "FROM listings WHERE active = 1 ORDER BY price_usdt_micros ASC"
    ).fetchall()
    events = conn.execute(
        "SELECT ts, type, name, detail FROM events ORDER BY id DESC LIMIT 100"
    ).fetchall()
    conn.close()

    rows_listings = "\n".join(
        f"<tr><td>{name}</td><td>{CLASS_NAMES.get(cid, cid)}</td><td>{RACE_NAMES.get(rid, rid)}</td>"
        f"<td>{lvl}</td><td>{price / 1e6:g} USDT</td><td>{'sí' if online else 'no'}</td>"
        f"<td>{first_seen}</td></tr>"
        for name, cid, rid, lvl, price, online, first_seen in listings
    )
    rows_events = "\n".join(
        f"<tr><td>{ts}</td><td>{type_}</td><td>{name or '-'}</td><td>{detail}</td></tr>"
        for ts, type_, name, detail in events
    )

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Mercado United Online — dashboard</title>
<style>
body {{ font-family: Segoe UI, sans-serif; margin: 24px; background: #f7f7f7; color: #222; }}
h1 {{ font-size: 1.4rem; }}
h2 {{ margin-top: 32px; font-size: 1.1rem; }}
table {{ border-collapse: collapse; width: 100%; background: #fff; }}
th, td {{ border: 1px solid #ddd; padding: 6px 10px; font-size: 0.85rem; text-align: left; }}
th {{ background: #eee; }}
.updated {{ color: #666; font-size: 0.8rem; }}
</style></head><body>
<h1>Mercado United Online — estado actual</h1>
<p class="updated">Última actualización: {now_iso()}</p>

<h2>Personajes activos en venta ({len(listings)})</h2>
<table>
<tr><th>Nombre</th><th>Clase</th><th>Raza</th><th>Nivel</th><th>Precio</th><th>Online</th><th>Publicado</th></tr>
{rows_listings}
</table>

<h2>Últimos eventos</h2>
<table>
<tr><th>Fecha</th><th>Tipo</th><th>Personaje</th><th>Detalle</th></tr>
{rows_events}
</table>
</body></html>"""
    DASHBOARD_PATH.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    run()
