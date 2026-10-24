#!/usr/bin/env python3
"""
理想個体ノート — データ自動更新スクリプト（GitHub Actions 月1実行想定）

やること（全自動・差分のみ）:
  1. PvPoke gamemaster.json と PokeAPI 日本語名CSV を取得
  2. 既存 pokemon_forms と突合し、未収録のタネ/地方種を検出、種族値変更も検出
  3. pokemon_species を upsert（dex/name_ja/family_id）
  4. 新規フォームを追加、種族値変更フォームを更新
  5. 追加/変更フォームの理想個体(3リーグ×rank20)を計算し ideal_ivs に投入
       （変更フォームは既存 ideal を削除してから再投入）

書き込みは service_role キー（環境変数 SERVICE_ROLE_KEY）で REST 直叩き。
計算ロジックは compute_ivs.py と同一（検証済み）。

環境変数:
  SB_URL             例) https://jhzukqbdlhmxnvkehxhg.supabase.co （未設定ならデフォルト）
  SERVICE_ROLE_KEY   Supabase の service_role キー（GitHub Secret 推奨）
  DRY_RUN=1          書き込みを行わず件数だけ表示（Supabase不要・取得と計算のみ）
"""
import json, math, csv, io, os, sys, urllib.request, urllib.error

SB_URL = os.environ.get("SB_URL", "https://jhzukqbdlhmxnvkehxhg.supabase.co").rstrip("/")
KEY = os.environ.get("SERVICE_ROLE_KEY", "")
REST = SB_URL + "/rest/v1"
DRY = os.environ.get("DRY_RUN", "") == "1"

GM_URL = "https://raw.githubusercontent.com/pvpoke/pvpoke/master/src/data/gamemaster.json"
NAMES_URL = "https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv/pokemon_species_names.csv"

# ---- CPM（整数レベル1..50。半レベルは sqrt((c1^2+c2^2)/2)）. 値は固定・検証済み ----
CPM_INT = [0.094,0.16639787,0.21573247,0.25572005,0.29024988,0.3210876,0.34921268,0.3752356,0.39956728,0.4225,0.44310755,0.4627984,0.48168495,0.49985844,0.51739395,0.5343543,0.5507927,0.5667545,0.5822789,0.5974,0.6121573,0.6265671,0.64065295,0.65443563,0.667934,0.6811649,0.69414365,0.7068842,0.7193991,0.7317,0.7377695,0.74378943,0.74976104,0.7556855,0.76156384,0.76739717,0.7731865,0.77893275,0.784637,0.7903,0.7953,0.8003,0.8053,0.8103,0.8153,0.8203,0.8253,0.8303,0.8353,0.8403]
def build_cpm(cap=50):
    cpm = {}
    for lv in range(1, cap+1): cpm[float(lv)] = CPM_INT[lv-1]
    for lv in range(1, cap):
        c1, c2 = CPM_INT[lv-1], CPM_INT[lv]; cpm[lv+0.5] = math.sqrt((c1*c1 + c2*c2)/2)
    return cpm
CPM = build_cpm(); LEVELS = sorted(CPM)
def cpf(a, d, h, m): return max(10, int(math.floor(a*math.sqrt(d)*math.sqrt(h)*(m*m)/10)))
def topn(ba, bd, bh, cap, n=20):
    combos = []
    for a in range(16):
        for d in range(16):
            for h in range(16):
                at, dt, ht = ba+a, bd+d, bh+h; chosen = None
                for lv in LEVELS:
                    m = CPM[lv]; c = cpf(at, dt, ht, m)
                    if c <= cap: chosen = (lv, m, c)
                    else: break
                if chosen is None: continue
                lv, m, c = chosen; sp = (at*m)*(dt*m)*math.floor(ht*m)
                combos.append((sp, c, a, d, h, lv))
    combos.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return combos[:n]
LG = [(1, 1500), (2, 2500), (3, 10000)]
def ideal_rows(form_id, ba, bd, bh):
    rows = []
    for lid, cap in LG:
        for rk, (sp, c, a, d, h, lv) in enumerate(topn(ba, bd, bh, cap), 1):
            lvv = int(lv) if float(lv).is_integer() else lv
            rows.append({"form_id": form_id, "league_id": lid, "rank": rk,
                         "atk_iv": a, "def_iv": d, "hp_iv": h, "level": lvv,
                         "cp": c, "stat_product": int(round(sp))})
    return rows

# ---- HTTP ----
def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "iv-updater"})
    with urllib.request.urlopen(req, timeout=180) as r: return json.loads(r.read().decode())
def get_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "iv-updater"})
    with urllib.request.urlopen(req, timeout=180) as r: return r.read().decode()
def sb(method, path, body=None, prefer=None):
    if DRY: return 0, "[dry]"
    url = REST + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"apikey": KEY, "Authorization": "Bearer " + KEY, "Content-Type": "application/json"}
    if prefer: headers["Prefer"] = prefer
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=180) as r: return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode()[:500]
        print(f"  !! {method} {path} -> {e.code}: {body_txt}", file=sys.stderr)
        raise

def sb_get_all(path_no_range):
    """公開読み取り（既存forms取得）。1000行ページング。"""
    out = []; frm = 0
    key = KEY or "anon"  # DRYでは使わない
    while True:
        url = f"{REST}{path_no_range}&limit=1000&offset={frm}"
        headers = {"apikey": KEY, "Authorization": "Bearer " + KEY}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=180) as r:
            chunk = json.loads(r.read().decode())
        out += chunk
        if len(chunk) < 1000: break
        frm += 1000
    return out

REGION_WORD = {"Alolan": "アローラ", "Galarian": "ガラル", "Hisuian": "ヒスイ", "Paldean": "パルデア"}
def paren(n): return n[n.rindex("(")+1:-1].strip() if n.endswith(")") and "(" in n else None

def build_candidates(gm):
    famByDex = {}
    for p in gm:
        dx = p["dex"]; fid = (p.get("family") or {}).get("id"); sid = p["speciesId"]
        if dx not in famByDex or len(sid) < famByDex[dx][1]: famByDex[dx] = (fid, len(sid))
    famByDex = {d: (v[0] or ("FAMILY_DEX_%d" % d)) for d, v in famByDex.items()}
    cands = {}; seen = set()
    for p in gm:
        tags = set(p.get("tags") or [])
        if tags & {"shadow", "mega", "supermega"}: continue
        name = p["speciesName"]
        if name in seen: continue
        pr = paren(name); region = None
        if pr in REGION_WORD: region = REGION_WORD[pr]
        elif pr is not None: continue
        seen.add(name)
        bs = p["baseStats"]; types = p["types"]
        t1 = types[0]; t2 = types[1] if len(types) > 1 and types[1] != "none" else None
        rar = None
        if "mythical" in tags: rar = "mythical"
        elif "legendary" in tags or "wildlegendary" in tags: rar = "legendary"
        if "ultrabeast" in tags: rar = "ultra_beast"
        cands[name] = dict(dex=p["dex"], region=region, t1=t1, t2=t2,
                           atk=bs["atk"], df=bs["def"], hp=bs["hp"], rar=rar)
    return cands, famByDex

def chunked(lst, n):
    for i in range(0, len(lst), n): yield lst[i:i+n]

def main():
    if not DRY and not KEY:
        print("SERVICE_ROLE_KEY が未設定です。GitHub Secret に SUPABASE_SERVICE_ROLE_KEY を登録してください。", file=sys.stderr)
        sys.exit(1)
    print("1) ソース取得 …")
    gm = get_json(GM_URL)["pokemon"]
    ja = {}
    for row in csv.DictReader(io.StringIO(get_text(NAMES_URL))):
        if row["local_language_id"] == "1": ja[int(row["pokemon_species_id"])] = row["name"]
    cands, famByDex = build_candidates(gm)
    print(f"   gamemaster {len(gm)}件 / 候補フォーム {len(cands)} / 日本語名 {len(ja)}")

    print("2) 既存フォーム取得 …")
    if DRY:
        existing = []
    else:
        existing = sb_get_all("/pokemon_forms?select=id,species_en,base_atk,base_def,base_hp&order=id")
    byName = {f["species_en"]: f for f in existing}
    maxid = max((f["id"] for f in existing), default=832)
    print(f"   既存フォーム {len(existing)}（最大id={maxid}）")

    print("3) pokemon_species upsert …")
    species_rows = [{"dex": dx, "name_ja": ja.get(dx), "family_id": famByDex.get(dx, "FAMILY_DEX_%d" % dx)}
                    for dx in range(1, 1026)]
    sb("POST", "/pokemon_species?on_conflict=dex", species_rows,
       prefer="resolution=merge-duplicates,return=minimal")

    print("4) 差分検出 …")
    new_forms = []; changed = []; nid = maxid
    for name, c in cands.items():
        ex = byName.get(name)
        if ex is None:
            nid += 1
            new_forms.append({"id": nid, "dex": c["dex"], "region": c["region"], "form": None,
                              "species_en": name, "type1": c["t1"], "type2": c["t2"],
                              "base_atk": c["atk"], "base_def": c["df"], "base_hp": c["hp"], "rarity": c["rar"]})
        else:
            if (ex["base_atk"], ex["base_def"], ex["base_hp"]) != (c["atk"], c["df"], c["hp"]):
                changed.append((ex["id"], c))
    print(f"   新規フォーム {len(new_forms)} / 種族値変更 {len(changed)}")

    if new_forms:
        print("5) 新規フォーム投入 …")
        for part in chunked(new_forms, 500):
            sb("POST", "/pokemon_forms?on_conflict=species_en", part,
               prefer="resolution=ignore-duplicates,return=minimal")
    for fid, c in changed:
        sb("PATCH", f"/pokemon_forms?id=eq.{fid}",
           {"base_atk": c["atk"], "base_def": c["df"], "base_hp": c["hp"],
            "type1": c["t1"], "type2": c["t2"], "rarity": c["rar"]})
        sb("DELETE", f"/ideal_ivs?form_id=eq.{fid}")

    print("6) 理想個体の計算・投入 …")
    targets = [(f["id"], f["base_atk"], f["base_def"], f["base_hp"]) for f in new_forms] \
            + [(fid, c["atk"], c["df"], c["hp"]) for fid, c in changed]
    if DRY: targets = targets[:2]  # DRYは計算の妥当性確認のみ（全件は回さない）
    all_rows = []
    for fid, ba, bd, bh in targets:
        all_rows += ideal_rows(fid, ba, bd, bh)
    print(f"   対象フォーム {len(targets)} / ideal行 {len(all_rows)}")
    for part in chunked(all_rows, 2000):
        sb("POST", "/ideal_ivs", part, prefer="return=minimal")

    print("完了。" + ("（DRY_RUN: 書き込みなし）" if DRY else ""))

if __name__ == "__main__":
    main()
