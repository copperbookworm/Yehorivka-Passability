import json, io, os
from pathlib import Path
import numpy as np
from PIL import Image

ROOT = Path(r'D:\mil-ai')
CAT = json.loads((ROOT/'tools'/'maps_catalog.json').read_text(encoding='utf-8'))
SRC = ROOT/'.tmp'/'maps_source'
OUT = Path(r'D:\mil-ai\IFC\ArtyCalc\data')
(OUT/'maps').mkdir(parents=True, exist_ok=True)
N = 1024
BASE = 2048

meta = []
for m in CAT:
    slug = m['slug']
    d = SRC/slug
    hp, bp = d/'heightmap.png', d/'basemap.webp'
    if not hp.exists() or not bp.exists():
        print('skip', slug); continue
    hm = Image.open(hp).convert('RGB')
    ha = np.asarray(hm).astype(np.int16)
    elev_full = (255 + ha[:, :, 0] - ha[:, :, 2]).astype(np.float32) * m['zscale']
    lo, hi = float(elev_full.min()), float(elev_full.max())
    if hi <= lo: hi = lo + 1.0
    elev = np.asarray(Image.fromarray(elev_full, mode='F').resize((N, N), Image.Resampling.BILINEAR)).astype(np.float32)
    q = np.clip((elev - lo) / (hi - lo) * 65535.0, 0, 65535).astype(np.uint16)
    Image.fromarray(q).save(OUT/'maps'/(slug + '_elev.png'), optimize=True)
    im = Image.open(bp).convert('RGB').resize((BASE, BASE), Image.Resampling.LANCZOS)
    im.save(OUT/'maps'/(slug + '_base.jpg'), quality=78, optimize=True, progressive=True)
    meta.append({'slug': slug, 'name': m['name'], 'label': m['label'],
                 'minx': m['minx'], 'minz': m['minz'], 'maxx': m['maxx'], 'maxz': m['maxz'],
                 'spanx': m['maxx']-m['minx'], 'spanz': m['maxz']-m['minz'],
                 'cellx': (m['maxx']-m['minx'])/N, 'cellz': (m['maxz']-m['minz'])/N,
                 'elevLo': lo, 'elevHi': hi,
                 'base': 'maps/' + slug + '_base.jpg', 'elev': 'maps/' + slug + '_elev.png'})
    print('ok', slug, round(lo,1), round(hi,1), (OUT/'maps'/(slug+'_base.jpg')).stat().st_size, (OUT/'maps'/(slug+'_elev.png')).stat().st_size)

(OUT/'maps.json').write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding='utf-8')
print('maps', len(meta))
