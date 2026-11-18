"""Builds the source files of a Canadian region's detailed map: derived/geo/regions/<code>/*.sqlite (CA-ON, CA-BC).

  derived/buildenv/bin/python tools/build_region_canada.py CA-ON all                     # every layer, whole province
  derived/buildenv/bin/python tools/build_region_canada.py CA-BC water places --bbox -123.5,48.4,-123.3,48.5
  derived/buildenv/bin/python tools/build_region_canada.py CA-ON check                    # tables and row counts
  then: packaging/build_packs.py map CA-ON, and packaging/state_maps.py CA-ON to publish it like a state map.

Layers, in the order `all` runs them. Each writes one file with the same tables and columns as the US tool named, so the
map, packaging/build_packs.py (region_set) and packaging/state_maps.py read it the same way:

  roads      national.sqlite   tools/build_map.py on Geofabrik OpenStreetMap extracts (ODbL): streets, land, water
                               lines and help points. Repeat --osm-gpkg for multiple local, unpacked GeoPackages.
  terrain    terrain.sqlite    tools/build_terrain.py on 1° cells cut from Natural Resources Canada's MRDEM-30 terrain
                               model (OGL – Canada; MRDEM is in part made from Copernicus WorldDEM-30, whose notice it
                               carries). Read through GDAL from NRCan's cloud-optimised files: only the box is fetched.
  water      water.sqlite      as tools/build_water.py. Ontario: OHN Waterbody and Watercourse and the Ontario Dam
                               Inventory (OGL – Ontario), from the Land Information Ontario map service. British Columbia:
                               Freshwater Atlas lakes, manmade waterbodies and named streams, and BC Dams (OGL – British
                               Columbia), from the BC Geographic Warehouse WFS. Dams keep name, type, height, year
                               and status; columns that name people (BC's owner and safety officer) are never read.
  crops      crops.sqlite      as tools/build_crops.py, from Agriculture and Agri-Food Canada's Annual Crop Inventory
                               (OGL – Canada): orchards, other fruit, vineyards and berries. --crop-geotiff uses a
                               local Annual Crop Inventory GeoTIFF instead of the image service.
  buildings  buildings.sqlite  as tools/build_buildings.py, from Statistics Canada's Open Database of Buildings v3
                               (OGL – Canada; each municipal source and its licence is listed in the download's
                               ODB_v3_data_providers.csv). --ms fills zoom-14 cells ODB leaves empty with Microsoft's
                               Canadian Building Footprints (ODbL). Use --ms-only to skip ODB and build entirely from
                               Microsoft footprints when ODB is unavailable. Only outlines and heights are kept. For OSM heights,
                               run tools/build_osm_heights.py with BUILDINGS_OUT set to this file afterwards.
  places     places.sqlite     as tools/build_places.py, from Natural Resources Canada's Canadian Geographical Names
                               (OGL – Canada) and named OpenStreetMap places in this region's national.sqlite.

Addresses are not built here: tools/build_addresses.py --country CA, then packaging/address_indexes.py (see their
headers). Every source, licence and attribution is listed in packaging/regions/<code>.json, which this tool reads.
This tool runs on a build machine and downloads public data into derived/geo/sources/canada/. It is never part of the
app, which only reads the files it writes. --bbox builds a smaller box (a test); the default is the region's box.
Needs numpy, tifffile, rasterio (GDAL) and pyproj, as the build environment has for the US tools.
"""
import argparse, csv, io, json, math, os, re, shutil, sqlite3, subprocess, sys, tempfile, time, urllib.parse, urllib.request, zipfile, zlib
from array import array
from contextlib import closing, contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
GEO = HERE / 'derived/geo'
CACHE = GEO / 'sources/canada'
sys.path.insert(0, str(HERE / 'packaging'))
sys.path.insert(0, str(HERE / 'tools'))
import regions                  # noqa: E402
import build_water as W         # noqa: E402  (pack, simplify, inside_point, ring_area_km2, wkb_polys, the water schema)

LAYERS = ['roads', 'terrain', 'water', 'crops', 'buildings', 'places']
AGENT = 'Lastbook map builder (offline library; github.com/Immortal-Protocols/lastbook)'
STEP = 0.5                       # fetch boxes this many degrees across, so no one request is too big


# ------------------------------------------------------------------------------------------------------------ fetching --
def get(url, params=None, tries=4):
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': AGENT}), timeout=180) as r:
                return r.read()
        except Exception as e:      # a busy public service: wait and try again
            if i == tries - 1:
                raise RuntimeError(f'{url[:160]}: {e}')
            time.sleep(5 * (i + 1))


def download(url, dest):
    """A whole file, kept in the cache; a finished file is not fetched again."""
    dest = Path(dest)
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + '.part')
    print('downloading', url, flush=True)
    req = urllib.request.Request(url, headers={'User-Agent': AGENT})
    with urllib.request.urlopen(req, timeout=600) as r, open(tmp, 'wb') as f:
        shutil.copyfileobj(r, f, 1 << 22)
    os.replace(tmp, dest)
    return dest


def cells(box, step=STEP):
    w, s, e, n = box
    y = s
    while y < n:
        x = w
        while x < e:
            yield (x, y, min(x + step, e), min(y + step, n))
            x += step
        y += step


def arcgis(layer, box, where='1=1', fields='*'):
    """Every feature of an ArcGIS MapServer layer in the box, as GeoJSON features in EPSG:4326, each once."""
    seen = set()
    info = json.loads(get(layer, {'f': 'json'}))
    page = int(info.get('maxRecordCount') or 1000)
    oid = next((f['name'] for f in info.get('fields', []) if f.get('type') == 'esriFieldTypeOID'), 'OBJECTID')
    if fields != '*':
        fields += ',' + oid        # the key that tells a feature met in two boxes
    for c in cells(box):
        start = 0
        while True:
            d = json.loads(get(layer + '/query', {'where': where, 'geometry': ','.join(map(str, c)), 'geometryType': 'esriGeometryEnvelope',
                                                  'inSR': 4326, 'spatialRel': 'esriSpatialRelIntersects', 'outFields': fields, 'outSR': 4326,
                                                  'returnGeometry': 'true', 'f': 'geojson', 'resultOffset': start, 'resultRecordCount': page}))
            feats = d.get('features') or []
            for f in feats:
                key = f.get('id') if f.get('id') is not None else (f.get('properties') or {}).get(oid)
                if key is not None and key in seen:
                    continue
                seen.add(key)
                yield f
            if len(feats) < page and not d.get('exceededTransferLimit') and not (d.get('properties') or {}).get('exceededTransferLimit'):
                break
            start += len(feats)


def wfs(endpoint, box, cql=None, props=None):
    """Every feature of a BC Geographic Warehouse WFS layer in the box (GeoJSON, EPSG:4326), each once."""
    base, q = endpoint.split('?', 1)
    layer = urllib.parse.parse_qs(q)['typeName'][0]
    seen = set()
    for c in cells(box):
        flt = f"BBOX(GEOMETRY,{c[0]},{c[1]},{c[2]},{c[3]},'EPSG:4326')" + (f' AND {cql}' if cql else '')
        start = 0
        while True:
            params = {'service': 'WFS', 'version': '2.0.0', 'request': 'GetFeature', 'typeName': layer, 'outputFormat': 'application/json',
                      'srsName': 'EPSG:4326', 'count': 5000, 'startIndex': start, 'sortBy': 'OBJECTID', 'CQL_FILTER': flt}
            if props:
                params['propertyName'] = ','.join(props + ['GEOMETRY'])
            d = json.loads(get(base, params))
            feats = d.get('features') or []
            for f in feats:
                if f['id'] in seen:
                    continue
                seen.add(f['id'])
                yield f
            if len(feats) < 5000:
                break
            start += len(feats)


def source(r, layer, word):
    """The region file's source for a layer whose dataset name holds this word."""
    return next(s for s in r['sources'][layer] if word.lower() in s['dataset'].lower())


# -------------------------------------------------------------------------------------------------------------- shapes --
def rings_of(geom):
    """GeoJSON (Multi)Polygon -> list of polygons, each a list of rings of (lon, lat)."""
    if not geom:
        return []
    polys = [geom['coordinates']] if geom['type'] == 'Polygon' else geom['coordinates'] if geom['type'] == 'MultiPolygon' else []
    return [[[(p[0], p[1]) for p in ring] for ring in poly] for poly in polys]


def lines_of(geom):
    if not geom:
        return []
    ls = [geom['coordinates']] if geom['type'] == 'LineString' else geom['coordinates'] if geom['type'] == 'MultiLineString' else []
    return [[(p[0], p[1]) for p in l] for l in ls]


def point_of(geom):
    if geom['type'] == 'Point':
        return geom['coordinates'][:2]
    pts = [p for l in lines_of(geom) for p in l] or [p for poly in rings_of(geom) for p in poly[0]]
    return pts[len(pts) // 2] if pts else None


def inside(box, lon, lat):
    return box[0] <= lon <= box[2] and box[1] <= lat <= box[3]


def finish(path):
    import gc
    gc.collect()                                  # the US tools leave their connections to the collector
    db = sqlite3.connect(path, timeout=60)
    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    db.execute('PRAGMA journal_mode=DELETE')      # one file, no -wal beside it, as build_packs copies it
    db.close()


# --------------------------------------------------------------------------------------------------------------- roads --
def build_roads(code, r, box, out, gpkg=None):
    extracts = []
    if not gpkg:
        urls = r['sources']['roads'][0]['access']
        for url in [urls] if isinstance(urls, str) else urls:
            z = download(url, CACHE / Path(urllib.parse.urlsplit(url).path).name)
            extract = CACHE / (z.name.removesuffix('.zip').replace('-latest-free.gpkg', '') + '.gpkg')
            if not extract.exists():
                tmp = extract.with_suffix('.part')
                with zipfile.ZipFile(z) as zf:
                    member = next(m for m in zf.namelist() if m.endswith('.gpkg'))
                    with zf.open(member) as src, open(tmp, 'wb') as dst:
                        shutil.copyfileobj(src, dst, 1 << 22)
                tmp.replace(extract)
            extracts.append(extract)
    else:
        extracts = gpkg
    dest = out / 'national.sqlite'
    subprocess.run([sys.executable, str(HERE / 'tools/build_map.py'), *map(str, extracts), str(dest)], check=True)
    db = sqlite3.connect(dest)
    # building outlines come from buildings.sqlite here, as in a state map; OSM's would draw twice
    db.execute("DELETE FROM feat_idx WHERE id IN (SELECT id FROM feat WHERE layer='bldg')")
    db.execute("DELETE FROM feat WHERE layer='bldg'")
    names = ', '.join(Path(extract).name for extract in extracts)
    db.executemany('INSERT OR REPLACE INTO meta VALUES (?,?)', [('source', f'OpenStreetMap contributors, via Geofabrik {names} (ODbL 1.0)'),
                                                                ('bounds', ','.join(f'{v:.2f}' for v in box)), ('data_as_of', ''),
                                                                ('built', time.strftime('%Y-%m-%dT%H:%M:%S'))])
    db.commit()
    db.close()
    finish(dest)


# ------------------------------------------------------------------------------------------------------------- terrain --
ARCSEC = 1 / 3600


def mrdem_cells(r, box, folder):
    """1° cells of MRDEM in EPSG:4326 at 1 arc-second, named and tagged like the USGS 3DEP cells build_terrain.py reads.
    Outside the box a cell holds no data, so a small box fetches only what it needs."""
    import numpy as np
    import rasterio
    import tifffile
    from rasterio.vrt import WarpedVRT
    from rasterio.windows import from_bounds
    url = source(r, 'terrain', 'MRDEM')['access']
    w, s, e, n = box
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR', GDAL_HTTP_USERAGENT=AGENT), rasterio.open('/vsicurl/' + url) as src, \
            WarpedVRT(src, crs='EPSG:4326', resampling=rasterio.enums.Resampling.bilinear, src_nodata=src.nodata, nodata=-32767) as v:
        for top in range(math.ceil(n), math.floor(s), -1):
            for left in range(math.floor(w), math.ceil(e)):
                name = f'n{top:02d}w{-left:03d}' if left < 0 else f'n{top:02d}e{left:03d}'
                path = folder / name / f'mrdem_{name}.tif'
                if path.exists():
                    continue
                size = 3602                                  # one pixel more on every side, so sampling at the edge needs no neighbour
                ox, oy = left - ARCSEC, top + ARCSEC
                a = np.full((size, size), -32767, np.float32)
                cw, cs, ce, cn = max(w, left), max(s, top - 1), min(e, left + 1), min(n, top)
                c0, c1 = max(0, int((cw - ox) / ARCSEC) - 1), min(size, int(math.ceil((ce - ox) / ARCSEC)) + 1)
                r0, r1 = max(0, int((oy - cn) / ARCSEC) - 1), min(size, int(math.ceil((oy - cs) / ARCSEC)) + 1)
                win = from_bounds(ox + c0 * ARCSEC, oy - r1 * ARCSEC, ox + c1 * ARCSEC, oy - r0 * ARCSEC, v.transform)
                a[r0:r1, c0:c1] = v.read(1, window=win, out_shape=(r1 - r0, c1 - c0))
                path.parent.mkdir(parents=True, exist_ok=True)
                tifffile.imwrite(path, a, compression='zlib', extratags=[(33550, 'd', 3, (ARCSEC, ARCSEC, 0.0)),
                                                                          (33922, 'd', 6, (0.0, 0.0, 0.0, ox, oy, 0.0))])
                print('MRDEM cell', name, flush=True)


def build_terrain(code, r, box, out, workers):
    folder = CACHE / 'mrdem' / (code if box == regions.box(code) else code + '-' + '_'.join(f'{v:.3f}' for v in box))
    mrdem_cells(r, box, folder)
    subprocess.run([sys.executable, str(HERE / 'tools/build_terrain.py'), '--src', str(folder), '--out', str(out / 'terrain.sqlite'),
                    '--source', 'Natural Resources Canada MRDEM-30 (OGL - Canada; in part Copernicus WorldDEM-30)',
                    '--workers', str(workers), '--bbox=' + ','.join(map(str, box))], check=True)
    finish(out / 'terrain.sqlite')


# --------------------------------------------------------------------------------------------------------------- water --
def water_poly(db, origin, rec_id, name, kind_hint, polys, detail, src_url):
    """Outlines into feat/feat_idx and one clickable point, as build_water.build_nhd does for an NHD waterbody."""
    area = sum(max(0.0, W.ring_area_km2(p[0]) - sum(W.ring_area_km2(h) for h in p[1:])) for p in polys if len(p[0]) >= 4)
    minz = max(5, min(14, round(14 - math.log2(max(area, 1e-5) ** 0.5 / 0.02))))
    tol = 0.00002 if minz >= 12 else 0.00006 if minz >= 9 else 0.0002
    cls = {'reservoir': 'reservoir', 'river': 'river', 'canal': 'river'}.get(kind_hint, 'water')
    for poly in polys:
        rings = [r2 for r2 in (W.simplify(rg, tol) for rg in poly) if len(r2) >= 4]
        if not rings:
            continue
        xs = [x for x, _ in rings[0]]; ys = [y for _, y in rings[0]]
        cur = db.execute('INSERT INTO feat(layer,cls,name,minz,geom,origin) VALUES (?,?,?,?,?,?)', ('water', cls, name or '', minz, W.pack(rings), origin))
        db.execute('INSERT INTO feat_idx VALUES (?,?,?,?,?,?,?)', (cur.lastrowid, min(xs), max(xs), min(ys), max(ys), minz, 30))
    kind = 'reservoir' if kind_hint == 'reservoir' else 'lake' if kind_hint in ('lake', 'pond') else None
    if kind and (name or area >= 0.004) and polys:
        lon, lat = W.inside_point(max((p[0] for p in polys), key=len))
        pond = kind == 'lake' and not name and area < 0.04
        W.add_point(db, origin, rec_id, 'pond' if pond else kind, name or '', lat, lon, {'area_acres': round(area * 247.105, 1), **detail}, src_url)
        return 1
    return 0


def water_line(db, origin, name, cls, lines):
    for l in lines:
        l = W.simplify(l, 0.00002)
        if len(l) < 2:
            continue
        xs = [x for x, _ in l]; ys = [y for _, y in l]
        minz = 10 if cls == 'river' else 13
        cur = db.execute('INSERT INTO feat(layer,cls,name,minz,geom,origin) VALUES (?,?,?,?,?,?)', ('waterway', cls, name or '', minz, W.pack([l]), origin))
        db.execute('INSERT INTO feat_idx VALUES (?,?,?,?,?,?,?)', (cur.lastrowid, min(xs), max(xs), min(ys), max(ys), minz, 30))


def build_water(code, r, box, out, all_streams=False):
    W.DB = out / 'water.sqlite'
    db = W.open_db()
    prov = r['name']
    counts = {}
    if code == 'CA-ON':
        s = source(r, 'water', 'Waterbody')
        origin = f'{code}:ohn-waterbody'
        W.forget(db, origin)
        n = 0
        for f in arcgis(s['access'], box, fields='OGF_ID,WATERBODY_TYPE,OFFICIAL_NAME_LABEL,PERMANENCY'):
            p = f['properties']
            kind = (p.get('WATERBODY_TYPE') or '').lower()
            detail = {'state': prov, 'water_type': {'Permanent': 'Year-round', 'Intermittent': 'Seasonal (may be dry)'}.get(p.get('PERMANENCY') or '', '')}
            n += water_poly(db, origin, f"ohn:{p.get('OGF_ID')}", (p.get('OFFICIAL_NAME_LABEL') or '').strip(), kind, rings_of(f['geometry']), detail, s['url'])
        counts[origin] = n
        s = source(r, 'water', 'Watercourse')
        origin = f'{code}:ohn-watercourse'
        W.forget(db, origin)
        where = "WATERCOURSE_TYPE='Stream'" + ('' if all_streams else ' AND OFFICIAL_NAME_LABEL IS NOT NULL')
        n = 0
        for f in arcgis(s['access'], box, where=where, fields='OGF_ID,OFFICIAL_NAME_LABEL,FLOW_CLASSIFICATION'):
            p = f['properties']
            name = (p.get('OFFICIAL_NAME_LABEL') or '').strip()
            water_line(db, origin, name, 'river' if re.search(r'\bRiver\b', name) else 'stream', lines_of(f['geometry']))
            n += 1
        counts[origin] = n
        s = source(r, 'water', 'Dam')
        origin = f'{code}:dams'
        W.forget(db, origin)
        n = 0
        for f in arcgis(s['access'], box, fields='OGF_ID,DAM_ID,DAM_NAME,DAM_OWNERSHIP'):
            p = f['properties']
            pt = point_of(f['geometry'])
            if not pt or not inside(box, *pt):
                continue
            detail = {k2: str(p[k]).strip() for k, k2 in (('DAM_OWNERSHIP', 'owner'),) if p.get(k) not in (None, '', 'Unknown')}
            detail['state'] = prov
            W.add_point(db, origin, f"ondam:{p.get('DAM_ID') or p.get('OGF_ID')}", 'dam', (p.get('DAM_NAME') or '').strip().title() or 'Unnamed dam',
                        pt[1], pt[0], detail, s['url'])
            n += 1
        counts[origin] = n
    elif code == 'CA-BC':
        for word, kind in (('Lakes', 'lake'), ('Manmade', 'reservoir')):
            s = source(r, 'water', word)
            origin = f'{code}:fwa-{word.lower()}'
            W.forget(db, origin)
            n = 0
            for f in wfs(s['access'], box, props=['WATERBODY_POLY_ID', 'WATERBODY_TYPE', 'GNIS_NAME_1']):
                p = f['properties']
                n += water_poly(db, origin, f"fwa:{p.get('WATERBODY_POLY_ID')}", (p.get('GNIS_NAME_1') or '').strip(), kind,
                                rings_of(f['geometry']), {'state': prov}, s['url'])
            counts[origin] = n
        s = source(r, 'water', 'Stream')
        origin = f'{code}:fwa-streams'
        W.forget(db, origin)
        n = 0
        for f in wfs(s['access'], box, cql=None if all_streams else 'GNIS_NAME IS NOT NULL', props=['LINEAR_FEATURE_ID', 'GNIS_NAME', 'STREAM_ORDER']):
            p = f['properties']
            name = (p.get('GNIS_NAME') or '').strip()
            water_line(db, origin, name, 'river' if re.search(r'\bRiver\b', name) or (p.get('STREAM_ORDER') or 0) >= 5 else 'stream', lines_of(f['geometry']))
            n += 1
        counts[origin] = n
        s = source(r, 'water', 'Dams')
        origin = f'{code}:dams'
        W.forget(db, origin)
        n = 0
        keep = {'DAM_TYPE': 'dam_type', 'COMMISSIONED_YEAR': 'year', 'DAM_OPERATION_CODE': 'status',
                'FAILURE_CONSEQUENCE': 'hazard', 'DAM_ALTERNATE_NAME': 'other_names'}
        props = ['DAM_FILE_NUMBER', 'DAM_NAME', 'DAM_HEIGHT'] + list(keep)       # never DAM_OWNER_NAME or DAM_SAFETY_OFFICER: they name people
        for f in wfs(s['access'], box, props=props):
            p = f['properties']
            pt = point_of(f['geometry'])
            if not pt or not inside(box, *pt):
                continue
            detail = {k2: str(p[k]).strip() for k, k2 in keep.items() if p.get(k) not in (None, '')}
            if p.get('DAM_HEIGHT'):
                detail['height_ft'] = str(round(float(p['DAM_HEIGHT']) * 3.28084, 1))
            detail['state'] = prov
            W.add_point(db, origin, f"bcdam:{p.get('DAM_FILE_NUMBER')}", 'dam', (p.get('DAM_NAME') or '').strip().title() or 'Unnamed dam',
                        pt[1], pt[0], detail, s['url'])
            n += 1
        counts[origin] = n
    else:
        raise SystemExit(f'{code}: no water sources in this tool')
    for origin, n in counts.items():
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (origin, json.dumps({'records': n, 'bbox': box, 'built': time.strftime('%Y-%m-%dT%H:%M:%S')})))
    db.commit()
    db.close()
    W.build_index()
    finish(W.DB)
    print('water', counts, flush=True)


# --------------------------------------------------------------------------------------------------------------- crops --
# Annual Crop Inventory code -> (crop, kind, RGB). Kinds as tools/build_crops.py: orchards, vineyards, berries.
ACI = {180: ('Fruits', 'orchard', (90, 150, 70)), 188: ('Orchards', 'orchard', (200, 40, 40)), 189: ('Other fruits', 'orchard', (240, 140, 90)),
       190: ('Vineyards', 'vineyard', (120, 50, 150)), 181: ('Berries', 'berries', (150, 30, 90)), 182: ('Blueberries', 'berries', (60, 70, 180)),
       183: ('Cranberries', 'berries', (170, 20, 50)), 185: ('Other berries', 'berries', (150, 30, 90))}
CROP_MAXZ, CROP_MINZ, BLOCK = 13, 7, 20


@contextmanager
def crop_reader(r, year, crop_geotiff):
    """Read crop windows on the same lon/lat grid from either source."""
    import rasterio
    if crop_geotiff is None:
        service = source(r, 'crops', 'Crop')['access'].format(year=year)

        def read_window(box, width, height):
            data = get(service + '/exportImage', {'bbox': ','.join(map(str, box)), 'bboxSR': 4326, 'imageSR': 4326,
                                                 'size': f'{width},{height}', 'format': 'tiff', 'pixelType': 'U8',
                                                 'interpolation': 'RSP_NearestNeighbor', 'f': 'image'})
            with rasterio.MemoryFile(data) as mf, mf.open() as ds:
                return ds.read(1), ds.transform

        yield read_window
        return

    import numpy as np
    from rasterio.transform import from_bounds
    from rasterio.warp import Resampling, reproject

    with rasterio.open(crop_geotiff, driver='GTiff') as ds:
        if not ds.crs or ds.count != 1 or ds.dtypes[0] != 'uint8':
            raise ValueError('--crop-geotiff needs a georeferenced Annual Crop Inventory GeoTIFF with one byte band')
        band = rasterio.band(ds, 1)

        def read_window(box, width, height):
            transform = from_bounds(*box, width, height)
            data = np.zeros((height, width), dtype=np.uint8)
            reproject(band, data, dst_transform=transform, dst_crs='EPSG:4326', dst_nodata=0,
                      resampling=Resampling.nearest)
            return data, transform

        yield read_window


def build_crops(r, box, out, year, crop_geotiff=None):
    with crop_reader(r, year, crop_geotiff) as read_window:
        write_crops(r, box, out, year, read_window)


def write_crops(r, box, out, year, read_window):
    import numpy as np
    import build_crops as C          # png_rgba and down, the same overlay as the US
    codes = sorted(ACI)
    lut = np.zeros(256, np.uint8)
    for i, c in enumerate(codes, 1):
        lut[c] = i
    palette = np.zeros((len(codes) + 1, 4), np.uint8)
    for i, c in enumerate(codes, 1):
        palette[i] = (*ACI[c][2], 190)
    dest = out / 'crops.sqlite'
    db = sqlite3.connect(dest)
    db.executescript('''PRAGMA journal_mode=WAL; DROP TABLE IF EXISTS resource; DROP TABLE IF EXISTS spatial; DROP TABLE IF EXISTS tile;
      CREATE TABLE resource(id INTEGER PRIMARY KEY, source_id TEXT, kind TEXT, name TEXT, lat REAL, lon REAL, detail TEXT, source TEXT);
      CREATE INDEX resource_kind ON resource(kind);
      CREATE VIRTUAL TABLE spatial USING rtree(id, minx, maxx, miny, maxy);
      CREATE TABLE tile(z INT, x INT, y INT, png BLOB, PRIMARY KEY(z,x,y)) WITHOUT ROWID;
      CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);''')
    nz = 2 ** CROP_MAXZ
    tiles, npts = {}, 0
    t = (np.arange(256) + 0.5) / 256
    for c in cells(box, 0.25):
        w, s, e, n = c
        width, height = max(1, round((e - w) / ARCSEC)), max(1, round((n - s) / ARCSEC))
        a, tr = read_window(c, width, height)
        cls = lut[a]
        rows, cols = np.nonzero(cls)
        if not rows.size:
            continue
        px_acres = (tr.a * 111320 * math.cos(math.radians((s + n) / 2))) * (-tr.e * 110574) / 4046.86
        br, bc = rows // BLOCK, cols // BLOCK
        key = (br.astype(np.int64) * 100000 + bc) * 32 + cls[rows, cols]
        u, cnt = np.unique(key, return_counts=True)
        cell, ci = u // 32, (u % 32).astype(int)
        order = np.lexsort((-cnt, cell))
        cell, ci, cnt = cell[order], ci[order], cnt[order]
        first = np.r_[True, cell[1:] != cell[:-1]]
        tot = np.add.reduceat(cnt, np.nonzero(first)[0])
        cell, ci, cnt = cell[first], ci[first], cnt[first]
        for cl, k, m, tt in zip(cell, ci, cnt, tot):
            if tt < 20:
                continue
            lon = tr.c + ((cl % 100000) * BLOCK + BLOCK / 2) * tr.a
            lat = tr.f + ((cl // 100000) * BLOCK + BLOCK / 2) * tr.e
            crop, kind, _ = ACI[codes[k - 1]]
            cur = db.execute('INSERT INTO resource(source_id,kind,name,lat,lon,detail,source) VALUES (?,?,?,?,?,?,?)',
                             (f'aci{year}:{w:.2f},{s:.2f}:{cl}', kind, crop, float(lat), float(lon),
                              json.dumps({'crop': crop, 'crop_acres': round(float(m) * px_acres, 1), 'perennial_acres': round(float(tt) * px_acres, 1), 'year': year}),
                              source(r, 'crops', 'Crop')['url']))
            db.execute('INSERT INTO spatial VALUES (?,?,?,?,?)', (cur.lastrowid, float(lon), float(lon), float(lat), float(lat)))
            npts += 1
        plon = tr.c + (cols[::7] + .5) * tr.a
        plat = tr.f + (rows[::7] + .5) * tr.e
        tx = ((plon + 180) / 360 * nz).astype(int)
        ty = ((1 - np.log(np.tan(np.radians(plat)) + 1 / np.cos(np.radians(plat))) / math.pi) / 2 * nz).astype(int)
        for x_, y_ in set(zip(tx.tolist(), ty.tolist())):
            lon_ = (x_ + t) / nz * 360 - 180
            lat_ = np.degrees(np.arctan(np.sinh(math.pi * (1 - 2 * (y_ + t) / nz))))
            cc = ((lon_ - tr.c) / tr.a).astype(int)
            rr = ((lat_ - tr.f) / tr.e).astype(int)
            RR, CC = np.meshgrid(rr, cc, indexing='ij')
            ok = (RR >= 0) & (RR < cls.shape[0]) & (CC >= 0) & (CC < cls.shape[1])
            v = np.zeros((256, 256), np.uint8)
            v[ok] = cls[RR[ok], CC[ok]]
            if v.any():
                tiles[(x_, y_)] = v if (x_, y_) not in tiles else np.where(tiles[(x_, y_)] > 0, tiles[(x_, y_)], v)
        db.commit()
    level = tiles
    for z in range(CROP_MAXZ, CROP_MINZ - 1, -1):
        for (x, y), v in level.items():
            db.execute('INSERT OR REPLACE INTO tile VALUES (?,?,?,?)', (z, x, y, C.png_rgba(palette[v])))
        if z > CROP_MINZ:
            nxt = {}
            for (x, y) in {(x // 2, y // 2) for x, y in level}:
                q = np.zeros((512, 512), np.uint8)
                for dy in (0, 1):
                    for dx in (0, 1):
                        v = level.get((2 * x + dx, 2 * y + dy))
                        if v is not None:
                            q[dy * 256:(dy + 1) * 256, dx * 256:(dx + 1) * 256] = v
                nxt[(x, y)] = C.down(q)
            level = nxt
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('built', json.dumps({'cells': npts, 'year': year, 'source': f'AAFC Annual Crop Inventory {year}',
                                                                                 'bbox': box, 'at': time.strftime('%Y-%m-%dT%H:%M:%S')})))
    db.commit()
    db.close()
    finish(dest)
    print(f'crops: {npts:,} clickable cells, {len(tiles):,} detailed tiles', flush=True)


# ----------------------------------------------------------------------------------------------------------- buildings --
WORLD = 2 ** 30
REACH = 1 << 12          # as tools/build_buildings.py


def world(lon, lat):
    s = math.sin(math.radians(max(-85, min(85, lat))))
    return int((lon + 180) / 360 * WORLD), int((0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * WORLD)


def add_building(tiles, ring, height_m):
    """One footprint into its z14 buckets, in the record format of tools/build_buildings.py: [npts, height_dm, X0, Y0, dX1, dY1, ...]."""
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 3:
        return None
    pts = [world(lon, lat) for lon, lat in ring]
    cx = sum(p[0] for p in pts) // len(pts); cy = sum(p[1] for p in pts) // len(pts)
    h = int(round(height_m * 10)) if height_m and height_m > 0 else 0
    rec = array('i', (len(pts), h, pts[0][0], pts[0][1]))
    lx, ly = pts[0]
    for x, y in pts[1:]:
        rec.extend((x - lx, y - ly)); lx, ly = x, y
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    keys = {(i, j) for i in range((min(xs) + REACH) >> 16, ((max(xs) - REACH) >> 16) + 1)
            for j in range((min(ys) + REACH) >> 16, ((max(ys) - REACH) >> 16) + 1)} | {(cx >> 16, cy >> 16)}
    for key in keys:
        a = tiles.get(key)
        if a is None:
            a = tiles[key] = [array('i'), 0]
        a[0].extend(rec); a[1] += 1
    return cx >> 16, cy >> 16


def metres(v, floors=None):
    try:
        h = float(v)
        if 2 <= h < 600:
            return h
    except (TypeError, ValueError):
        pass
    try:
        f = float(floors)
        return f * 3.2 + 1.5 if 0 < f < 130 else 0
    except (TypeError, ValueError):
        return 0


def build_buildings(code, r, box, out, odb_files=None, ms=False, ms_only=False):
    if ms_only and odb_files:
        raise ValueError('Microsoft-only builds cannot use ODB files')
    files = [Path(f) for f in odb_files or []]
    if not ms_only and not files:
        src = source(r, 'buildings', 'Open Database of Buildings')
        files = [download(src['access'].format(file=f), CACHE / 'odb' / f) for f in src['files']]
    microsoft_zip = None
    if ms or ms_only:
        src = next(s for s in r['sources']['buildings'] if s['publisher'] == 'Microsoft')
        url = src['access']
        microsoft_zip = download(url, CACHE / 'msbuildings-ca' / Path(urllib.parse.urlsplit(url).path).name)
    dest = out / 'buildings.sqlite'
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        target = dest if dest.exists() else Path(tmp) / dest.name
        with closing(sqlite3.connect(target)) as db, db:
            write_buildings(db, box, files, microsoft_zip)
        finish(target)
        if target != dest:
            target.replace(dest)


def write_buildings(db, box, files, microsoft_zip):
    db.executescript('PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; BEGIN; DROP TABLE IF EXISTS bldg; DROP TABLE IF EXISTS bldg_socal;'
                     'CREATE TABLE bldg(x INT, y INT, part INT, n INT, data BLOB, PRIMARY KEY(x,y,part)) WITHOUT ROWID;'
                     'CREATE TABLE bldg_socal(x INT, y INT, part INT, n INT, data BLOB, PRIMARY KEY(x,y,part)) WITHOUT ROWID;'
                     'CREATE TABLE IF NOT EXISTS bldg_osm(x INT, y INT, n INT, data BLOB, PRIMARY KEY(x,y)) WITHOUT ROWID;'
                     'CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);')
    if files:
        from pyproj import Transformer
        to_ll = Transformer.from_crs(3347, 4326, always_xy=True)
        to_lcc = Transformer.from_crs(4326, 3347, always_xy=True)
        edge = [(box[0] + (box[2] - box[0]) * i / 20, box[1] + (box[3] - box[1]) * j / 20) for i in range(21) for j in range(21)]
        ex, ey = to_lcc.transform([p[0] for p in edge], [p[1] for p in edge])
        env = (min(ex), max(ex), min(ey), max(ey))
    total, filled = 0, set()
    for part, f in enumerate(files):
        tiles, n = {}, 0
        with tempfile.TemporaryDirectory(dir=CACHE) as tmp:
            if f.suffix == '.zip':
                with zipfile.ZipFile(f) as zf:
                    gp = Path(zf.extract(next(m for m in zf.namelist() if m.endswith('.gpkg')), tmp))
            else:
                gp = f
            g = sqlite3.connect(f'file:{gp}?mode=ro', uri=True)
            table, col = g.execute("SELECT table_name, column_name FROM gpkg_geometry_columns").fetchone()
            q = (f'SELECT t.{col}, t.height, t.floors FROM "rtree_{table}_{col}" i JOIN "{table}" t ON t.fid=i.id '
                 'WHERE i.maxx>=? AND i.minx<=? AND i.maxy>=? AND i.miny<=?')
            for blob, height, floors in g.execute(q, (env[0], env[1], env[2], env[3])):
                if blob is None:
                    continue
                for poly in W.wkb_polys(blob):
                    xs, ys = to_ll.transform([p[0] for p in poly[0]], [p[1] for p in poly[0]])
                    ring = list(zip(xs, ys))
                    lon, lat = sum(xs) / len(xs), sum(ys) / len(ys)
                    if not inside(box, lon, lat):
                        continue
                    if add_building(tiles, ring, metres(height, floors)):
                        n += 1
            g.close()
        db.executemany('INSERT OR REPLACE INTO bldg VALUES (?,?,?,?,?)', ((x, y, part, c, zlib.compress(buf.tobytes(), 6)) for (x, y), (buf, c) in tiles.items()))
        filled |= set(tiles)
        total += n
        print(f'{f.name}: {n:,} buildings', flush=True)
    sources = ['Statistics Canada Open Database of Buildings v3 (OGL - Canada)'] if total else []
    if microsoft_zip:
        tiles, n = {}, 0
        with zipfile.ZipFile(microsoft_zip) as zf, zf.open(next(x for x in zf.namelist() if x.endswith('json'))) as fh:
            for line in io.TextIOWrapper(fh, encoding='utf-8'):
                line = line.strip().rstrip(',')
                if not line.startswith('{') or '"Polygon"' not in line:
                    continue
                try:
                    ring = json.loads(line)['geometry']['coordinates'][0]
                except (ValueError, KeyError, IndexError):
                    continue
                lon = sum(p[0] for p in ring) / len(ring); lat = sum(p[1] for p in ring) / len(ring)
                cx, cy = world(lon, lat)
                if not inside(box, lon, lat) or (cx >> 16, cy >> 16) in filled:
                    continue        # ODB has this cell: Microsoft only fills cells it leaves empty
                if add_building(tiles, [(p[0], p[1]) for p in ring], 0):
                    n += 1
        db.executemany('INSERT OR REPLACE INTO bldg VALUES (?,?,?,?,?)', ((x, y, 100, c, zlib.compress(buf.tobytes(), 6)) for (x, y), (buf, c) in tiles.items()))
        total += n
        if n:
            sources.append('Microsoft Canadian Building Footprints (ODbL)')
        print(f'Microsoft: {n:,} buildings', flush=True)
    db.executemany('INSERT OR REPLACE INTO meta VALUES (?,?)', [('buildings', str(total)), ('built', time.strftime('%Y-%m-%dT%H:%M:%S')),
                    ('source', ' and '.join(sources))])


# -------------------------------------------------------------------------------------------------------------- places --
TOWNS = {'CITY', 'TOWN', 'VILG', 'UNP', 'HAM', 'MUN2', 'IR'}


def cgn_rows(r, box, csv_text):
    """Canadian Geographical Names rows -> place rows (name, kind, detail, lat, lon, rank, source), as build_places.py ranks GNIS."""
    for row in csv.DictReader(io.StringIO(csv_text)):
        try:
            lat, lon = float(row['Latitude']), float(row['Longitude'])
        except (KeyError, ValueError):
            continue
        if not inside(box, lon, lat) or row.get('Generic Category') == 'Undersea and Maritime Feature':
            continue
        concise, where = row.get('Concise Code', ''), ', '.join(x for x in (row.get('Location'), r['name']) if x)
        if concise in TOWNS:
            yield row['Geographical Name'], 'town or neighborhood', where, lat, lon, 3, 'Natural Resources Canada (CGN)'
        elif concise == 'MUN1':
            yield row['Geographical Name'], 'county', where, lat, lon, 2, 'Natural Resources Canada (CGN)'
        else:
            yield row['Geographical Name'], (row.get('Generic Term') or 'place').lower(), where, lat, lon, 4, 'Natural Resources Canada (CGN)'


def build_places(code, r, box, out):
    import struct
    s = source(r, 'places', 'Geographical Names')
    z = download(s['access'], CACHE / 'cgn' / Path(urllib.parse.urlsplit(s['access']).path).name)
    with zipfile.ZipFile(z) as zf:
        text = zf.read(next(m for m in zf.namelist() if m.endswith('.csv'))).decode('utf-8-sig')
    dest = out / 'places.sqlite'
    dest.unlink(missing_ok=True)
    db = sqlite3.connect(dest)
    db.executescript('''CREATE TABLE place(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, detail TEXT, lat REAL, lon REAL, rank INTEGER, source TEXT);
      CREATE VIRTUAL TABLE place_fts USING fts5(name, detail, content='place', content_rowid='id', tokenize='unicode61');''')
    ins = 'INSERT INTO place(name,kind,detail,lat,lon,rank,source) VALUES (?,?,?,?,?,?,?)'
    db.executemany(ins, cgn_rows(r, box, text))
    n_cgn = db.execute('SELECT count(*) FROM place').fetchone()[0]
    n_osm = 0
    nat = out / 'national.sqlite'
    if nat.exists():                # named OpenStreetMap places and help points, as build_places.py reads map.sqlite
        src = sqlite3.connect(f'file:{nat}?mode=ro', uri=True)
        where = f"{r['name']} (OpenStreetMap)"
        for cls, name, lon, lat in src.execute("SELECT cls,name,lon,lat FROM poi WHERE name IS NOT NULL AND name<>''"):
            if inside(box, lon, lat):
                db.execute(ins, (name, cls.replace('_', ' '), where, lat, lon, 4, 'OpenStreetMap')); n_osm += 1
        for cls, name, g in src.execute("SELECT cls,name,geom FROM feat WHERE layer='place' AND name IS NOT NULL"):
            _, _, x, y = struct.unpack_from('<iiii', g)
            if inside(box, x / 1e6, y / 1e6):
                db.execute(ins, (name, cls, where, y / 1e6, x / 1e6, 1, 'OpenStreetMap')); n_osm += 1
        src.close()
    db.execute("INSERT INTO place_fts(place_fts) VALUES ('rebuild')")
    db.commit()
    db.close()
    finish(dest)
    print(f'places: {n_cgn:,} from CGN, {n_osm:,} from OpenStreetMap', flush=True)


# --------------------------------------------------------------------------------------------------------------- check --
def check(out):
    for name in ('national', 'water', 'terrain', 'crops', 'buildings', 'places'):
        p = out / f'{name}.sqlite'
        if not p.exists():
            print(f'{name}: not built')
            continue
        db = sqlite3.connect(f'file:{p}?mode=ro', uri=True)
        tables = [t for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                  if not re.search(r'_(node|parent|rowid|data|idx_data|content|docsize|config)$', t)]
        print(f'{name}: ' + ', '.join(f'{t} {db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]:,}' for t in tables))
        db.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('code', choices=[c for c in regions.codes() if regions.get(c)['country'] == 'CA'])
    ap.add_argument('layers', nargs='+', choices=LAYERS + ['all', 'check'])
    ap.add_argument('--bbox', help='W,S,E,N: a smaller box than the region (default: the region box with its margin)')
    ap.add_argument('--out', help='default derived/geo/regions/<code>')
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--year', type=int, default=2025, help='Annual Crop Inventory year')
    ap.add_argument('--crop-geotiff', type=Path, help='local Annual Crop Inventory GeoTIFF for --year (instead of the image service)')
    ap.add_argument('--osm-gpkg', action='append', help='a local, unpacked Geofabrik GeoPackage (repeat for multiple extracts)')
    odb = ap.add_mutually_exclusive_group()
    odb.add_argument('--odb', nargs='*', help='local ODB_v3_*.zip or .gpkg files instead of downloading them')
    odb.add_argument('--ms-only', action='store_true', help='use only Microsoft Canadian Building Footprints, skipping Statistics Canada')
    ap.add_argument('--ms', action='store_true', help='fill cells ODB leaves empty with Microsoft Canadian Building Footprints')
    ap.add_argument('--all-streams', action='store_true', help='every stream, not only named ones (much larger)')
    a = ap.parse_args()
    if a.crop_geotiff is not None and not a.crop_geotiff.is_file():
        ap.error('--crop-geotiff must name an existing local file')
    r = regions.get(a.code)
    box = tuple(map(float, a.bbox.split(','))) if a.bbox else regions.box(a.code)
    out = Path(a.out) if a.out else GEO / 'regions' / a.code
    out.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    layers = LAYERS if 'all' in a.layers else [x for x in LAYERS if x in a.layers]
    for layer in layers:
        t0 = time.time()
        print(f'== {a.code} {layer} {box}', flush=True)
        if layer == 'roads':
            build_roads(a.code, r, box, out, a.osm_gpkg)
        elif layer == 'terrain':
            build_terrain(a.code, r, box, out, a.workers)
        elif layer == 'water':
            build_water(a.code, r, box, out, a.all_streams)
        elif layer == 'crops':
            build_crops(r, box, out, a.year, a.crop_geotiff)
        elif layer == 'buildings':
            build_buildings(a.code, r, box, out, a.odb, a.ms, a.ms_only)
        elif layer == 'places':
            build_places(a.code, r, box, out)
        print(f'== {layer} done in {time.time() - t0:.0f}s', flush=True)
    if 'check' in a.layers or 'all' in a.layers:
        check(out)


if __name__ == '__main__':
    main()
