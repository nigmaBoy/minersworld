from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import firebase_admin
from firebase_admin import credentials, firestore
import os
import json

app = Flask(__name__)
CORS(app)

# ── Firebase init ──────────────────────────────────────────────────────────────
# On Render: set FIREBASE_SERVICE_ACCOUNT env var to the JSON string of your
# service account key. Locally: put serviceAccount.json next to this file.
if os.environ.get("FIREBASE_SERVICE_ACCOUNT"):
    sa = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])
    cred = credentials.Certificate(sa)
else:
    cred = credentials.Certificate("serviceAccount.json")

firebase_admin.initialize_app(cred)
db = firestore.client()

# ── Your claykrs auth token (set as env var on Render, never hardcode in prod) ─
MW_TOKEN = "eaeaf3f39bb456f1de67cabd5569fca874a5cdcb364e2cfcb44aac9795eb264385376102b589f998ca738ab623cb5f42784de5b3da66091b06cbc4920697cf2638c533772694a70b49e106d1f325a1e3d244f55afb6cd0c5da38ca54f3cc7ded9b0dcd3e677fb73b69a5e20430c3993acdb1449b753b7d24a5592c5fb25d3045"
MW_PROOF = "4d569c9476b95ae7af0a29ac3e969e6d3d3ea83ffcef2aa72a263ba459475504"

# ── The set of ore IDs your site actually tracks ───────────────────────────────
# These are derived from data.js — every id that appears in myOreData
# (all rarities EXCEPT Explosives, which stay manual).
# Generated from data.js by listing every id in every non-Explosives rarity.
TRACKED_ORE_IDS = set([
    # Legendary
    "12","13","26","27","41","42","56","57","70","71","85","86","100","101",
    "116","117","132","133","147","148","162","163","176","177","190","191",
    "204","205","218","219","232","233","246","247","262","263","277","278",
    "293","294","309","310","325","326","341","342","357","358","376","377",
    "391","392","407","413","419","426","431","437","442","447","489","490",
    "491","501","507","513","518","522","527","533","538","543","548","553",
    "558","562","567","583","587","591","593","612","613","614","634","635",
    "636","656","657","658","697","698","699","734","735","736","759","760",
    "761","782","783","784","805","806","807","828","829","830","852","853",
    "854","889","890","891",
    # Mythic
    "14","28","43","58","72","87","102","118","134","149","164","178","192",
    "206","220","234","248","264","279","295","311","327","343","359","360",
    "378","393","395","408","414","420","421","427","432","438","443","448",
    "492","493","494","502","508","514","519","523","528","534","539","549",
    "554","559","563","568","584","588","594","615","616","637","638","659",
    "660","700","701","737","738","762","763","785","786","808","809","831",
    "832","855","856","892","893",
    # Ethereal
    "361","396","397","398","399","400","401","402","403","404","409","410",
    "415","416","422","423","428","429","433","434","439","440","444","445",
    "449","450","451","452","453","454","455","456","457","458","459","460",
    "461","462","463","464","465","466","467","468","469","470","471","472",
    "473","474","475","495","496","497","617","618","639","640","661","662",
    "702","703","739","740","764","765","787","788","810","811","833","834",
    "857","858","894","895",
    # Celestial
    "663","664","665","666","667","668","669","670","671","672","673","674",
    "675","676","677","678","679","680","681","704","705","706","707","708",
    "709","710","711","712","714","715","717","741","766","789","812","835",
    "859","896",
    # Zenith
    "713","716","718","743","860",
    # Divine
    "836",
    # Nil
    "742",
])


def get_roblox_userid(username: str) -> str:
    """Resolve a Roblox username to a numeric userid."""
    resp = requests.post(
        "https://users.roblox.com/v1/usernames/users",
        json={"usernames": [username], "excludeBannedUsers": False},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    users = data.get("data", [])
    if not users:
        raise ValueError(f"Roblox user '{username}' not found")
    return str(users[0]["id"])


def fetch_claykrs_inventory(roblox_userid: str) -> dict:
    """
    Hit claykrs.com/mw/inventory with our stored token.
    Returns the raw JSON response.
    """
    url = f"https://claykrs.com/mw/inventory?userid={roblox_userid}"
    headers = {
        "accept": "*/*",
        "authorization": MW_TOKEN,
        "cookie": f"mw_token={MW_TOKEN}; mw_proof={MW_PROOF}",
        "referer": "https://claykrs.com/",
        "x-mw-auth-proof": MW_PROOF,
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0"
        ),
    }
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code == 403:
        raise PermissionError("claykrs returned 403 — token may be expired")
    resp.raise_for_status()
    return resp.json()


def map_inventory_to_site(raw_blocks: list) -> dict:
    """
    Convert claykrs blocks array [{id, count}, ...] into the site's
    inventory format {str(id): count} — only for IDs your site tracks.
    Explosives are excluded (they stay manual).
    """
    inventory = {}
    for block in raw_blocks:
        block_id = str(block.get("id", ""))
        count = int(block.get("count", 0))
        if block_id in TRACKED_ORE_IDS and count > 0:
            inventory[block_id] = count
    return inventory


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/sync-inventory", methods=["POST"])
def sync_inventory():
    """
    Body: { "uid": "<firebase_uid>", "roblox_username": "<username>" }

    1. Resolve roblox username → userid via Roblox API
    2. Fetch inventory from claykrs using our token
    3. Map to site ore IDs
    4. Write to Firebase users/{uid}/inventory (merge so prices/discord are kept)
    5. Return the new inventory + roblox_userid
    """
    body = request.get_json(silent=True) or {}
    uid = str(body.get("uid", "")).strip()
    roblox_username = str(body.get("roblox_username", "")).strip()

    if not uid:
        return jsonify({"error": "missing uid"}), 400
    if not roblox_username:
        return jsonify({"error": "missing roblox_username"}), 400

    # 1. Resolve Roblox userid
    try:
        roblox_userid = get_roblox_userid(roblox_username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": f"Roblox lookup failed: {e}"}), 502

    # 2. Fetch from claykrs
    try:
        raw = fetch_claykrs_inventory(roblox_userid)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except Exception as e:
        return jsonify({"error": f"claykrs fetch failed: {e}"}), 502

    raw_blocks = raw.get("blocks", [])
    if not isinstance(raw_blocks, list):
        return jsonify({"error": "unexpected claykrs response format"}), 502

    # 3. Map to site inventory
    inventory = map_inventory_to_site(raw_blocks)

    # 4. Write to Firebase — merge so we DON'T overwrite prices/discord/itemPrices
    try:
        db.collection("users").document(uid).set(
            {"inventory": inventory, "roblox_userid": roblox_userid},
            merge=True,
        )
    except Exception as e:
        return jsonify({"error": f"Firebase write failed: {e}"}), 500

    # 5. Return
    return jsonify({
        "ok": True,
        "roblox_userid": roblox_userid,
        "synced_count": len(inventory),
        "inventory": inventory,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)