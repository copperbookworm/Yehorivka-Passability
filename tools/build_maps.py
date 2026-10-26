"""Generate one self-contained HTML page per official SquadCalc map.

Source assets are downloaded into .tmp/maps_source/<slug>/ by tools/download_maps.py.
Output: android/YehorivkaMap/app/src/main/assets/maps/<slug>.html
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((ROOT / "tools" / "maps_catalog.json").read_text(encoding="utf-8"))
TEMPLATE = ROOT / "yehorivka-vehicle-passability.html"
SOURCE = ROOT / ".tmp" / "maps_source"
ANN = ROOT / "annotations"
OUT = ROOT / "android" / "YehorivkaMap" / "app" / "src" / "main" / "assets" / "maps"
N = 1024
BASE_SIZE = 1536
TOPO_SIZE = 1536
FLAGS_DIR = ROOT / "tools" / "_flags"
FLAGS_CN_FILE = ROOT / "tools" / "flags_cn.json"
FLAG_MODE_TYPE = {"AAS": "aas", "RAAS": "raas", "Invasion": "inv"}


def load_flags_cn() -> dict:
    try:
        return json.loads(FLAGS_CN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


FLAGS_CN = load_flags_cn()


def load_flag_db(name: str) -> dict:
    """本图的旗点图层：{"<rawName>": {m,v,t,c:[cap],l:{names,order}}}；cap = [名称, 序号名, x, z, 几何, 序号, 主基地]"""
    db = {}
    if not FLAGS_DIR.is_dir():
        return db
    for path in sorted(FLAGS_DIR.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(d.get("map") or "").lower() != name.lower() or not d.get("caps") or not d.get("raw"):
            continue
        caps = []
        for c in d["caps"]:
            caps.append([
                c.get("n") or c.get("id") or "",
                c.get("disp") or c.get("n") or "",
                round(float(c["x"]), 1),
                round(float(c["z"]), 1),
                c.get("sh") or [],
                c.get("ord") or 0,
                c.get("main") or 0,
            ])
        gm = d.get("gm") or "AAS"
        ver = int("".join(ch for ch in str(d.get("ver") or "") if ch.isdigit()) or 1)
        ent = {"m": gm, "v": ver, "t": FLAG_MODE_TYPE.get(gm, "aas"), "c": caps}
        lanes = d.get("lanes")
        if ent["t"] == "raas" and isinstance(lanes, dict) and lanes.get("order"):
            ent["l"] = {"names": lanes.get("names") or list(lanes["order"].keys()),
                        "order": lanes["order"]}
        db[d["raw"]] = ent
    return db


def flags_js(db: dict) -> tuple[str, str]:
    names = set()
    for ent in db.values():
        for cap in ent["c"]:
            names.add(cap[0])
    cn = {k: FLAGS_CN[k] for k in sorted(names) if k in FLAGS_CN}
    return (json.dumps(db, ensure_ascii=False, separators=(",", ":")),
            json.dumps(cn, ensure_ascii=False, separators=(",", ":")))

STRUCT_KINDS = {
    "box", "house", "monoslopehouse", "monoslopehousedoor", "toppedbox",
    "notchedapartment", "facadehouse", "fourstoryapartment", "threestorysmallapartment",
    "industrialoffice", "lshape", "lshapehouse", "lshapehouse2", "tshape",
    "doublegable", "cylinder", "chimney", "log", "lshapebox", "lshapebox2",
    "logpile", "bunker", "tubehangar",
}


def data_url(img: Image.Image, fmt: str = "PNG", quality: int = 88) -> str:
    buf = io.BytesIO()
    if fmt == "JPEG":
        img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
        mime = "image/jpeg"
    else:
        img.save(buf, format="PNG", optimize=True)
        mime = "image/png"
    return f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def data_url_webp(img: Image.Image, quality: int = 80) -> str:
    """整幅等高线地形图用 WebP：比同质量 JPEG 小 15-20%，细节更干净。"""
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=quality, method=6)
    return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def keep_large(mask, min_cells=120):
    """Drop small isolated water blobs; keep rivers, lakes and the sea."""
    h, w = mask.shape
    seen = np.zeros((h, w), dtype=bool)
    out = np.zeros((h, w), dtype=bool)
    for y0 in range(h):
        for x0 in range(w):
            if not mask[y0, x0] or seen[y0, x0]:
                continue
            stack = [(y0, x0)]
            seen[y0, x0] = True
            cells = []
            while stack:
                y, x = stack.pop()
                cells.append((y, x))
                if y > 0 and mask[y-1, x] and not seen[y-1, x]:
                    seen[y-1, x] = True; stack.append((y-1, x))
                if y + 1 < h and mask[y+1, x] and not seen[y+1, x]:
                    seen[y+1, x] = True; stack.append((y+1, x))
                if x > 0 and mask[y, x-1] and not seen[y, x-1]:
                    seen[y, x-1] = True; stack.append((y, x-1))
                if x + 1 < w and mask[y, x+1] and not seen[y, x+1]:
                    seen[y, x+1] = True; stack.append((y, x+1))
            if len(cells) >= min_cells:
                for y, x in cells:
                    out[y, x] = True
    return out


def qrot(q, v):
    x, y, z, w = q
    vx, vy, vz = v
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return (
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    )


def process_map(m: dict) -> dict:
    slug = m["slug"]
    src = SOURCE / slug
    hpath = src / "heightmap.png"
    bpath = src / "basemap.webp"
    if not hpath.exists() or not bpath.exists():
        raise SystemExit(f"missing source for {slug}")
    spanx = m["maxx"] - m["minx"]
    spanz = m["maxz"] - m["minz"]
    cellx = spanx / N
    cellz = spanz / N
    cell = (cellx + cellz) / 2

    # ---- elevation / water ----
    # The source heightmap is 1 m/px.  Resize the encoded R/B image first for
    # terrain, but derive the water mask from the full-resolution elevation:
    # narrow rivers would disappear if the water mask were also downsampled.
    hm = Image.open(hpath).convert("RGB")
    hm_size = hm.size
    ha = np.asarray(hm).astype(np.int16)
    elev_full = (255 + ha[:, :, 0] - ha[:, :, 2]).astype(np.float32) * m["zscale"]
    lo = float(elev_full.min())
    hi = float(elev_full.max())
    if hi <= lo:
        hi = lo + 1.0
    elev_small = np.asarray(
        Image.fromarray(elev_full, mode="F").resize((N, N), Image.Resampling.BILINEAR)
    ).astype(np.float32)
    wm_full = Image.fromarray(((elev_full <= 1.5).astype(np.uint8) * 255), mode="L")
    water_elev = np.asarray(wm_full.resize((N, N), Image.Resampling.BOX)) > 1
    del hm, ha, elev_full, wm_full

    # ---- slope ----
    gy, gx = np.gradient(elev_small, cellz, cellx)
    slope = np.degrees(np.arctan(np.hypot(gx, gy))).clip(0, 85).astype(np.float32)

    # ---- base ----
    base = Image.open(bpath).convert("RGB").resize((BASE_SIZE, BASE_SIZE), Image.Resampling.LANCZOS)
    base_small = base.resize((N, N), Image.Resampling.LANCZOS)
    tpath = src / "topomap.webp"
    topo_out = None
    if tpath.exists():
        topo_src = Image.open(tpath).convert("RGB")
        topo_small = topo_src.convert("L").resize((N, N), Image.Resampling.LANCZOS)
        # 0.32.0：另存一份彩色地形图给页面做底图切换
        topo_out = topo_src.resize((TOPO_SIZE, TOPO_SIZE), Image.Resampling.LANCZOS)
        del topo_src
    else:
        topo_small = None

    # ---- roads from local brightness on the basemap ----
    gray = base_small.convert("L")
    local = gray.filter(ImageFilter.BoxBlur(5))
    ga = np.asarray(gray).astype(np.int16)
    la = np.asarray(local).astype(np.int16)
    road = ((ga > la + 12) & (slope < 8.0)).astype(np.uint8) * 255

    # ---- 3D instances: trees, buildings, walls ----
    tree_img = Image.new("L", (N, N), 0)
    build_img = Image.new("L", (N, N), 0)
    wall_img = Image.new("L", (N, N), 0)
    bridge_img = Image.new("L", (N, N), 0)
    tdraw = ImageDraw.Draw(tree_img)
    bdraw = ImageDraw.Draw(build_img)
    wdraw = ImageDraw.Draw(wall_img)
    brdraw = ImageDraw.Draw(bridge_img)

    tj = src / "3d" / "trees.json"
    tb = src / "3d" / "trees.bin"
    tree_count = 0
    tcount = np.zeros((N, N), dtype=np.int32)
    build_count = 0
    wall_count = 0
    if tj.exists() and tb.exists():
        spec = json.loads(tj.read_text(encoding="utf-8"))
        blob = tb.read_bytes()
        for sp in spec.get("species", []):
            kind = sp.get("kind", "")
            label = (sp.get("label") or "").lower()
            is_tree = kind in ("tree", "bush")
            is_wall = any(k in label for k in ("wall", "fence", "barrier", "hedge"))
            is_bridge = any(k in label for k in ("bridge", "viaduct", "culvert"))
            is_build = (kind in STRUCT_KINDS) and not is_wall and not is_bridge
            if not (is_tree or is_wall or is_build or is_bridge):
                continue
            lay = sp.get("layout", {}).get("matrices")
            if not lay:
                continue
            off = int(lay.get("byteOffset", 0))
            cnt = int(sp.get("instanceCount", 0))
            cnt = min(cnt, int(lay.get("byteLength", 0)) // 40)
            if cnt <= 0 or off + cnt * 40 > len(blob):
                continue
            mats = np.frombuffer(blob, dtype="<f4", count=cnt * 10, offset=off).reshape(cnt, 10)
            bmin = sp.get("bboxMin")
            bmax = sp.get("bboxMax")
            if is_tree:
                if bmin and bmax:
                    w = max(0.5, abs(bmax[0] - bmin[0]))
                    d = max(0.5, abs(bmax[2] - bmin[2]))
                else:
                    w = d = 6.0 if kind == "tree" else 3.0
                for row in mats:
                    pos = row[0:3]
                    q = row[3:7]
                    sc = row[7:10]
                    gx = (pos[0] - m["minx"]) / spanx * N
                    gy = (pos[2] - m["minz"]) / spanz * N
                    rx = max(0.7, w * abs(float(sc[0])) / 2.0 / cellx)
                    ry = max(0.7, d * abs(float(sc[2])) / 2.0 / cellz)
                    if gx < -rx or gy < -ry or gx > N + rx or gy > N + ry:
                        continue
                    tdraw.ellipse([gx - rx, gy - ry, gx + rx, gy + ry], fill=255)
                    ix = min(N - 1, max(0, int(gx)))
                    iy = min(N - 1, max(0, int(gy)))
                    tcount[iy, ix] += 1
                    tree_count += 1
            else:
                if not (bmin and bmax):
                    continue
                bx0, bz0, bx1, bz1 = float(bmin[0]), float(bmin[2]), float(bmax[0]), float(bmax[2])
                for row in mats:
                    pos = row[0:3]
                    q = row[3:7]
                    sc = row[7:10]
                    pts = []
                    for lx, lz in ((bx0, bz0), (bx1, bz0), (bx1, bz1), (bx0, bz1)):
                        vx, vy, vz = qrot(q, (lx * sc[0], 0.0, lz * sc[2]))
                        wx = pos[0] + vx
                        wz = pos[2] + vz
                        pts.append(((wx - m["minx"]) / spanx * N, (wz - m["minz"]) / spanz * N))
                    if is_bridge:
                        brdraw.polygon(pts, fill=255)
                    elif is_wall:
                        wdraw.polygon(pts, fill=255)
                        wall_count += 1
                    else:
                        bdraw.polygon(pts, fill=255)
                        build_count += 1

    # 25 m window tree cover, like the original data
    rad = max(1, int(round(25.0 / cell / 2.0)))
    blurred = tree_img.filter(ImageFilter.BoxBlur(rad))
    cover = (np.asarray(blurred).astype(np.float32) / 255.0 * 250.0).clip(0, 250)
    build = np.asarray(build_img)
    obst = np.where(build > 0, 255, cover).astype(np.uint8)
    # 0.29.0：每格树的棵数（0..250 饱和），供非「路线」模块使用
    treecnt8 = np.clip(tcount, 0, 250).astype(np.uint8)

    # ---- water and bridges ----
    # SquadCalc's topographic layer tints water almost black while land stays
    # green/tan.  It is far more reliable than trying to guess water colour
    # from the basemap (rivers range from blue to grey to olive).
    rgb = np.asarray(base_small).astype(np.int16)
    rr, gg, bb = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    mx = np.maximum(np.maximum(rr, gg), bb)
    mn = np.minimum(np.minimum(rr, gg), bb)
    blue = (bb > rr + 10) & (bb > gg + 3) & (ga < 175)
    green = (gg > rr + 10) & (bb > rr - 12) & (ga < 115)
    neutral_maps = {"yehorivka", "gorodok", "narva", "goosebay"}
    if slug in neutral_maps:
        sat = (mx - mn) / np.maximum(mx, 1)
        neutral = (sat < 0.36) & (ga < 165)
    else:
        neutral = np.zeros_like(blue)
    if topo_small is not None:
        tgray = np.asarray(topo_small).astype(np.int16)
        water = (tgray < 105) & (blue | green | neutral)
    else:
        water = np.zeros((N, N), dtype=bool)
    water &= (ga > 18)
    water &= (np.asarray(build_img) == 0)
    water &= (cover < 90)
    wm = Image.fromarray((water.astype(np.uint8) * 255), mode="L")
    water = np.asarray(wm.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))) > 127
    water = keep_large(water, 120)
    # Bridge decks are road-coloured surfaces adjacent to water; also catch
    # explicit 3D bridge instances.  They override water in classify().
    wm = Image.fromarray((water.astype(np.uint8) * 255), mode="L")
    near = np.asarray(wm.filter(ImageFilter.MaxFilter(5))) > 0
    bridge = ((road > 0) & near) | (np.asarray(bridge_img) > 0)

    # Manual annotations from tools/annotate_maps.py override the automatic
    # masks.  Files are 2048x2048; downsample to the app's 1024 grid.
    manual_water = (ANN / slug / "water.png").exists()
    manual_bridge = (ANN / slug / "bridge.png").exists()
    manual_boundary = (ANN / slug / "boundary.png").exists()
    manual_clear = (ANN / slug / "clear_road.png").exists()
    manual_obstacle = (ANN / slug / "obstacle.png").exists()
    manual_boat = (ANN / slug / "boat.png").exists()
    if manual_water:
        im = Image.open(ANN / slug / "water.png").convert("L").resize((N, N), Image.Resampling.BOX)
        water = np.asarray(im) > 8
    if manual_bridge:
        im = Image.open(ANN / slug / "bridge.png").convert("L").resize((N, N), Image.Resampling.BOX)
        bridge = np.asarray(im) > 0
    # No boundary file => the whole map is inside.
    boundary = np.full((N, N), 255, dtype=np.uint8)
    if manual_boundary:
        im = Image.open(ANN / slug / "boundary.png").convert("L").resize((N, N), Image.Resampling.BOX)
        boundary = np.where(np.asarray(im) > 8, 255, 0).astype(np.uint8)
    clear_road = np.zeros((N, N), dtype=np.uint8)
    if manual_clear:
        im = Image.open(ANN / slug / "clear_road.png").convert("L").resize((N, N), Image.Resampling.BOX)
        clear_road = np.where(np.asarray(im) > 0, 255, 0).astype(np.uint8)
    manual_obst = np.zeros((N, N), dtype=np.uint8)
    if manual_obstacle:
        im = Image.open(ANN / slug / "obstacle.png").convert("L").resize((N, N), Image.Resampling.BOX)
        manual_obst = np.where(np.asarray(im) > 0, 255, 0).astype(np.uint8)
    boat = np.zeros((N, N), dtype=np.uint8)
    if manual_boat:
        im = Image.open(ANN / slug / "boat.png").convert("L").resize((N, N), Image.Resampling.BOX)
        boat = np.where(np.asarray(im) > 0, 255, 0).astype(np.uint8)

    elev8 = ((elev_small - lo) / (hi - lo) * 255.0).clip(0, 255).astype(np.uint8)
    slope8 = (slope / 85.0 * 255.0).clip(0, 255).astype(np.uint8)
    wall8 = np.asarray(wall_img).astype(np.uint8)
    road8 = road.astype(np.uint8)
    water8 = (water.astype(np.uint8) * 255)
    bridge8 = (bridge.astype(np.uint8) * 255)

    def png(arr):
        return data_url(Image.fromarray(arr, mode="L"))

    return dict(
        slug=slug,
        label=m["label"],
        minx=m["minx"], maxx=m["maxx"], minz=m["minz"], maxz=m["maxz"],
        spanx=spanx, spanz=spanz, lo=lo, hi=hi,
        base=data_url(base, "JPEG", 82),
        topo=(data_url_webp(topo_out) if topo_out is not None else data_url(base, "JPEG", 82)),
        slope=png(slope8), elev=png(elev8), obst=png(obst),
        wall=png(wall8), road=png(road8), water=png(water8), bridge=png(bridge8),
        bound=png(boundary), clear=png(clear_road), mblock=png(manual_obst), boat=png(boat),
        treecnt=png(treecnt8),
        trees=tree_count, builds=build_count, walls=wall_count,
        water_cells=int(water.sum()), bridge_cells=int(bridge.sum()),
        manual_water=manual_water, manual_bridge=manual_bridge,
        manual_boundary=manual_boundary, manual_clear=manual_clear, manual_obstacle=manual_obstacle,
        manual_boat=manual_boat,
        hm_size=hm_size,
    )


def selector_html(current: str) -> str:
    opts = []
    for m in CATALOG:
        sel = " selected" if m["slug"] == current else ""
        opts.append(f'<option value="{m["slug"]}"{sel}>{m["label"]}</option>')
    return (
        '<div class="mapSwitch"><span>地图</span>'
        '<select id="mapSelect">' + "".join(opts) + "</select></div>"
    )


def make_page(m: dict, data: dict | None) -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    label = m["label"]
    text = re.sub(r"<title>.*?</title>", f"<title>{label} 载具通行性 · 路线规划</title>", text, count=1)
    text = text.replace(
        '<h1>Yehorivka <span>载具通行 · 路线</span></h1>',
        f'<h1>{label} <span>载具通行 · 路线</span></h1>',
    )
    sub = f'地形 {data["spanx"]/1000:.2f} × {data["spanz"]/1000:.2f} km · 高程 1 m/px · Squad 10.6'
    text = re.sub(r'<div class="sub">.*?</div>', f'<div class="sub">{sub}</div>', text, count=1)
    # 0.24.0：地图选择放到「文件」栏（fileBarMount）之后 —— 文件栏在地图选项上边
    anchor_bar = '<div id="fileBarMount"></div>'
    if anchor_bar not in text:
        raise SystemExit("template is missing " + anchor_bar)
    text = text.replace(
        anchor_bar,
        anchor_bar + '\n      ' + selector_html(m["slug"]),
        1,
    )
    text = re.sub(
        r"数据：SquadCalc 公开的 Yehorivka[^\n]*",
        f"数据：SquadCalc 公开的 {label} 高程图（{data['hm_size'][0]}×{data['hm_size'][1]}，1 m/px）与 3D 物件实例。",
        text, count=1,
    )
    text = re.sub(
        r"var MINX = [^\n]+;",
        f'var MINX = {data["minx"]:.2f}, MAXX = {data["maxx"]:.2f}, MINZ = {data["minz"]:.2f}, MAXZ = {data["maxz"]:.2f};',
        text, count=1,
    )
    text = re.sub(
        r"var ELEV_LO = [^\n]+;",
        f'var ELEV_LO = {data["lo"]:.2f}, ELEV_HI = {data["hi"]:.2f};',
        text, count=1,
    )
    for key, url in (
        ("IMG_BASE", data["base"]), ("IMG_TOPO", data["topo"]),
        ("IMG_SLOPE", data["slope"]), ("IMG_ELEV", data["elev"]),
        ("IMG_OBST", data["obst"]), ("IMG_WALL", data["wall"]), ("IMG_ROAD", data["road"]),
        ("IMG_WATER", data["water"]), ("IMG_BRIDGE", data["bridge"]),
        ("IMG_BOUND", data["bound"]), ("IMG_CLEAR", data["clear"]), ("IMG_MBLOCK", data["mblock"]),
        ("IMG_BOAT", data["boat"]), ("IMG_TREE", data["treecnt"]),
    ):
        text = re.sub(rf'var {key} = "[^"]*";', f'var {key} = "{url}";', text, count=1)

    fdb = load_flag_db(m["name"])
    if "var FLAG_DB = " in text:
        db_js, cn_js = flags_js(fdb)
        text = re.sub(r"var FLAG_DB = [^\n]*;",
                      lambda _m: "var FLAG_DB = " + db_js + ";", text, count=1)
        text = re.sub(r"var FLAG_CN = [^\n]*;",
                      lambda _m: "var FLAG_CN = " + cn_js + ";", text, count=1)

    css = (
        ".mapSwitch{display:flex;align-items:center;gap:8px;margin:0 0 12px}"
        ".mapSwitch span{font-size:11px;color:#8b97a8;white-space:nowrap}"
        ".mapSwitch select{flex:1;min-width:0;background:#0d141b;border:1px solid #243040;"
        "color:#d9e1ea;border-radius:5px;padding:7px;font-family:inherit;font-size:12px}"
    )
    text = text.replace("</style>", css + "</style>", 1)
    script = (
        "<script>(function(){var s=document.getElementById('mapSelect');"
        f"var cur='{m['slug']}';"
        "function sync(){if(s.value!==cur)s.value=cur;}"
        "sync();window.addEventListener('pageshow',sync);"
        "document.addEventListener('visibilitychange',function(){if(!document.hidden)sync();});"
        "s.addEventListener('change',function(){if(s.value)location.href=s.value+'.html';});})();</script>"
    )
    text = text.replace("</body>", script + "</body>", 1)
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="one slug, or empty for all")
    ap.add_argument("--skip-existing", action="store_true", help="do not regenerate existing pages")
    args = ap.parse_args()
    wanted = [m for m in CATALOG if not args.only or m["slug"] == args.only]
    if not wanted:
        raise SystemExit("no map")
    OUT.mkdir(parents=True, exist_ok=True)
    for m in wanted:
        dest = OUT / f"{m['slug']}.html"
        if args.skip_existing and dest.exists():
            print("skip", m["slug"], flush=True)
            continue
        print("processing", m["slug"], flush=True)
        data = process_map(m)
        page = make_page(m, data)
        print("  trees", data["trees"], "builds", data["builds"], "walls", data["walls"],
              "water", data["water_cells"], "bridges", data["bridge_cells"],
              "manual", (data["manual_water"], data["manual_bridge"], data["manual_boundary"],
                          data["manual_clear"], data["manual_obstacle"], data["manual_boat"]), flush=True)
        (OUT / f"{m['slug']}.html").write_text(page, encoding="utf-8")
        print("  wrote", (OUT / f"{m['slug']}.html").stat().st_size, flush=True)


if __name__ == "__main__":
    main()
