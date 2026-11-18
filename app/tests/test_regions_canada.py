"""Ontario and British Columbia map sets: region files, boxes, address search and the Canadian address rows.

No network and no library: the region files are read from packaging/regions, and the address indexes are made here.
"""
import importlib.util
import json
import re
import sqlite3
import sys
import tempfile
import unittest
import zipfile
import zlib
from array import array
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from engine import address_search
from engine.address_search import find_addresses, normalize
from engine.setup import manifest

ROOT = Path(__file__).resolve().parents[2]
REGIONS = ROOT / 'packaging' / 'regions'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


regions = load('regions', REGIONS / '__init__.py')


class RegionFiles(unittest.TestCase):
    def test_loader_lists_both_provinces(self):
        self.assertIn('CA-ON', regions.codes())
        self.assertIn('CA-BC', regions.codes())
        self.assertTrue(regions.is_region('CA-ON'))
        self.assertFalse(regions.is_region('CA'))          # California stays a state
        self.assertFalse(regions.is_region('ON'))
        self.assertEqual(regions.get('CA-ON')['name'], 'Ontario')
        self.assertEqual(regions.get('CA-BC')['name'], 'British Columbia')

    def test_files_are_valid_json_with_every_key(self):
        for code in ('CA-ON', 'CA-BC'):
            with self.subTest(code=code):
                r = json.loads((REGIONS / f'{code}.json').read_text(encoding='utf-8'))
                regions.check(r, code)
                self.assertEqual((r['country'], r['kind']), ('CA', 'province'))
                for key in ('id', 'name', 'country', 'kind', 'bbox', 'bbox_source', 'includes', 'sources', 'address'):
                    self.assertIn(key, r)
                self.assertEqual(set(r['sources']), {'terrain', 'water', 'crops', 'buildings', 'places', 'roads', 'addresses'})
                for layer, group in r['sources'].items():
                    self.assertTrue(group, layer)
                    for s in group:
                        for key in ('dataset', 'publisher', 'url', 'licence', 'licence_url', 'attribution'):
                            self.assertTrue(s.get(key), f'{layer}: {s.get("dataset")} has no {key}')
                        self.assertTrue(s['url'].startswith('https://'))

    def test_boxes(self):
        expected = {'CA-ON': ((-95.5, -94.5), (41.5, 42.0), (-74.5, -74.0), (56.5, 57.0)),
                    'CA-BC': ((-139.5, -138.5), (48.0, 48.5), (-114.5, -113.5), (59.5, 60.5))}
        for code, ranges in expected.items():
            with self.subTest(code=code):
                bbox = regions.get(code)['bbox']
                for value, (lo, hi) in zip(bbox, ranges):
                    self.assertTrue(lo <= value <= hi, f'{code} {bbox}')
                w, s, e, n = regions.box(code)
                self.assertAlmostEqual(w, bbox[0] - .15)
                self.assertAlmostEqual(n, bbox[3] + .15)

    def test_postcode_rules(self):
        on, bc = regions.get('CA-ON')['address'], regions.get('CA-BC')['address']
        self.assertEqual(on['postcode_first_letters'], ['K', 'L', 'M', 'N', 'P'])
        self.assertEqual(bc['postcode_first_letters'], ['V'])
        for code in ('M5H 2N2', 'K2G 0P7', 'V8W 2C3', 'V6B1A1'):
            self.assertRegex(code, on['postcode_pattern'])
        for code in ('90210', 'D1A 1A1', 'M5H-2N2'):
            self.assertNotRegex(code, on['postcode_pattern'])

    def test_people_columns_are_never_read(self):
        """BC Dams names owners and safety officers; the builder must not ask for those columns."""
        text = (ROOT / 'tools' / 'build_region_canada.py').read_text(encoding='utf-8')
        props = re.search(r"props = \[.*?\] \+ list\(keep\)", text).group(0)
        self.assertNotIn('OWNER', props)
        self.assertNotIn('SAFETY', props)
        self.assertNotIn('DAM_OWNER_NAME', re.search(r'keep = \{.*?\}', text, re.S).group(0))


class PackHooks(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / 'packaging'))
        try:
            import build_packs
        except (OSError, ImportError) as e:     # the pack tools need a POSIX libc
            self.skipTest(f'build_packs does not load here: {e}')
        self.bp = build_packs

    def tearDown(self):
        sys.path.remove(str(ROOT / 'packaging'))

    def test_state_box_takes_a_region_box(self):
        self.assertEqual(self.bp.state_box(None, 'CA-ON'), regions.box('CA-ON'))   # no ZIP centres are read

    def test_credits_carry_the_licences(self):
        text = '\n'.join(self.bp.region_credits())
        for line in ('Open Government Licence – Canada', 'Open Government Licence – Ontario', 'Open Government Licence – British Columbia',
                     'Copernicus WorldDEM-30', '© OpenStreetMap contributors'):
            self.assertIn(line, text)
        self.assertNotRegex(text, r'https?://')


class CanadianAddresses(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.geo = Path(self.temp.name)
        (self.geo / 'addresses').mkdir()
        self.write('CA-ON', [('100', 'Queen Street West', 'Toronto', 'CA-ON', 'M5H 2N2', 43.6525, -79.3832),
                             ('100', 'Queen Street', 'Ottawa', 'CA-ON', 'K1P 1J9', 45.4215, -75.6972)])
        self.write('CA-BC', [('1000', 'Douglas Street', 'Victoria', 'CA-BC', 'V8W 2C3', 48.4262, -123.3656),
                             ('453', '12th Ave W', 'Vancouver', 'CA-BC', 'V5Y 1V4', 49.261, -123.113),
                             ('453', '12th Ave E', 'Vancouver', 'CA-BC', 'V5T 0B5', 49.260, -123.095),
                             ('123', 'East First Avenue', 'Vancouver', 'CA-BC', 'V5T 1A1', 49.269, -123.103)])
        self.write('CA', [('100', 'Euclid Avenue', 'Ontario', 'CA', '91762', 34.06, -117.65),
                          ('18001', 'Nine Mile Road', 'Example', 'CA', '90001', 34.0, -118.0)])
        self.write('MI', [('18001', 'W Nine Mile Road', 'Novi', 'MI', '48375', 42.45, -83.47)])

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, rows):
        db = sqlite3.connect(self.geo / 'addresses' / f'{name}.sqlite')
        try:
            db.execute('CREATE TABLE addr(num TEXT,street TEXT,norm TEXT,city TEXT,state TEXT,zip TEXT,lat REAL,lon REAL)')
            db.executemany('INSERT INTO addr VALUES(?,?,?,?,?,?,?,?)', [(n, s, normalize(s), c, st, z, la, lo) for n, s, c, st, z, la, lo in rows])
            db.commit()
        finally:
            db.close()

    def test_ontario_address_with_province_and_postcode(self):
        for query in ('100 Queen St W, Toronto, ON M5H 2N2', '100 Queen Street West, Toronto, Ontario', '100 queen st w toronto on m5h2n2',
                      '100 Queen St W M5H 2N2', '100 Queen St W, Toronto ON'):
            with self.subTest(query=query):
                result = find_addresses(query, self.geo)
                self.assertEqual(len(result), 1, result)
                self.assertEqual(result[0]['name'], '100 Queen Street West')
                self.assertEqual(result[0]['detail'], 'Toronto, ON, M5H 2N2')

    def test_british_columbia_postcodes(self):
        for query in ('1000 Douglas St, Victoria, BC V8W 2C3', '1000 Douglas Street V8W2C3', '1000 Douglas St, Victoria, British Columbia'):
            with self.subTest(query=query):
                result = find_addresses(query, self.geo)
                self.assertEqual([r['lat'] for r in result], [48.4262])
        self.assertEqual(find_addresses('1000 Douglas St, Victoria, BC V8W 9Z9', self.geo), [])   # a wrong code does not fall back

    def test_province_parts(self):
        self.assertEqual(address_search._parts('100 Queen St W, Toronto, ON M5H 2N2'), ('100', ['queen', 'st', 'w', 'toronto'], 'CA-ON', 'M5H 2N2'))
        self.assertEqual(address_search._parts('1000 Douglas St V8W 2C3')[2:], ('CA-BC', 'V8W 2C3'))

    def test_canadian_street_directions_can_go_before_or_after_the_street(self):
        for query in ('453 W 12th Avenue', '453 12th Ave W,', '453 West 12th Avenue, Vancouver, BC V5Y 1V4'):
            with self.subTest(query=query):
                self.assertEqual([r['detail'] for r in find_addresses(query, self.geo)], ['Vancouver, BC, V5Y 1V4'])
        self.assertEqual([r['name'] for r in find_addresses('123 1st Ave E, Vancouver, BC', self.geo)], ['123 East First Avenue'])
        self.assertEqual(find_addresses('453 W 12th Avenue, Vancouver, BC V5T 0B5', self.geo), [])

    def test_us_behaviour_is_unchanged(self):
        self.assertEqual(address_search._parts('18001 W Nine Mile Road, Novi, MI 48375'), ('18001', ['w', 'nine', 'mile', 'rd', 'novi'], 'MI', '48375'))
        self.assertEqual(address_search._parts('18001 nine mile Novi 48375'), ('18001', ['nine', 'mile', 'novi'], '', '48375'))
        result = find_addresses('18001 nine mile CA', self.geo)          # California's index only, never CA-ON
        self.assertEqual([r['detail'] for r in result], ['Example, CA, 90001'])
        result = find_addresses('18001 W Nine Mile Road, Novi, MI 48375', self.geo)
        self.assertEqual([r['detail'] for r in result], ['Novi, MI, 48375'])

    def test_ontario_california_without_an_ontario_index(self):
        (self.geo / 'addresses' / 'CA-ON.sqlite').unlink()
        result = find_addresses('100 Euclid Ave Ontario', self.geo)      # read as a city, as before, when no province index is here
        self.assertEqual([r['detail'] for r in result], ['Ontario, CA, 91762'])


class AddressRows(unittest.TestCase):
    def setUp(self):
        self.tool = load('build_addresses_under_test', ROOT / 'tools' / 'build_addresses.py')

    def test_canadian_postcode_is_kept_whole(self):
        row = self.tool.row('CA', ' 1500 ', 'Riverside Drive', 'OTTAWA', 'ON', 'k2g0p7', 45.38, -75.71)
        self.assertEqual(row, ('1500', 'Riverside Drive', 'riverside dr', 'Ottawa', 'CA-ON', 'K2G 0P7', 45.38, -75.71))
        self.assertEqual(self.tool.row('CA', '1', 'Main St', 'Victoria', 'British Columbia', 'V8W 2C3', 48, -123)[4:6], ('CA-BC', 'V8W 2C3'))
        self.assertEqual(self.tool.region_code('CA', 'Québec'), 'CA-QC')
        self.assertEqual(self.tool.region_code('CA', ''), '')

    def test_us_rows_are_unchanged(self):
        row = self.tool.row('US', '18001', 'W Nine Mile Road', 'novi', 'mi', '48375', 42.45, -83.47)
        self.assertEqual(row, ('18001', 'W Nine Mile Road', 'w nine mile rd', 'Novi', 'MI', '48375', 42.45, -83.47))
        self.assertEqual(self.tool.region_code('US', 'California'), 'CA')    # two letters of whatever Overture gives, as before


class ManifestIds(unittest.TestCase):
    def manifest_with(self, address_id):
        entry = {'id': address_id, 'title': 'x', 'available': True, 'installed_bytes': 1,
                 'files': [{'name': f'Lastbook-Addresses-{address_id}.zip', 'url': f'https://downloads.lastbook.org/addresses/{address_id}.zip',
                            'size': 1, 'sha256': '0' * 64}]}
        return {'version': 1, 'packs': [], 'models': [], 'maps': [], 'address_indexes': [entry]}

    def test_region_address_index_ids(self):
        self.assertIsNotNone(manifest.validate(self.manifest_with('CA-ON')))
        self.assertIsNotNone(manifest.validate(self.manifest_with('MI')))
        self.assertIsNone(manifest.validate(self.manifest_with('ca-on')))
        self.assertIsNone(manifest.validate(self.manifest_with('ONTARIO')))


class BuilderPieces(unittest.TestCase):
    """The parts of tools/build_region_canada.py that need no download."""
    def setUp(self):
        self.tool = load('build_region_canada_under_test', ROOT / 'tools' / 'build_region_canada.py')

    def test_names_become_place_rows(self):
        text = ('CGNDB ID,Geographical Name,Generic Term,Generic Category,Concise Code,Latitude,Longitude,Location,Province - Territory\n'
                'A,Ottawa,City,Populated Place,CITY,45.42,-75.69,,Ontario\n'
                'B,Dows Lake,Lake,Water Feature,LAKE,45.40,-75.70,Ottawa,Ontario\n'
                'C,Far Away,City,Populated Place,CITY,49.0,-90.0,,Ontario\n')
        rows = list(self.tool.cgn_rows(regions.get('CA-ON'), (-75.8, 45.3, -75.6, 45.5), text))
        self.assertEqual([(r[0], r[1], r[5]) for r in rows], [('Ottawa', 'town or neighborhood', 3), ('Dows Lake', 'lake', 4)])
        self.assertEqual(rows[1][2], 'Ottawa, Ontario')

    def test_building_record_matches_the_us_format(self):
        tiles = {}
        ring = [(-75.70, 45.40), (-75.6999, 45.40), (-75.6999, 45.4001), (-75.70, 45.4001), (-75.70, 45.40)]
        key = self.tool.add_building(tiles, ring, 12.5)
        buf, n = tiles[key]
        rec = array('i', zlib.decompress(zlib.compress(buf.tobytes())))
        self.assertEqual((n, rec[0], rec[1]), (1, 4, 125))                 # four points (closing point dropped), height in decimetres
        self.assertEqual((rec[2] >> 16, rec[3] >> 16), key)

    def test_geojson_shapes(self):
        poly = {'type': 'MultiPolygon', 'coordinates': [[[[0, 0, 5], [1, 0, 5], [1, 1, 5], [0, 0, 5]]]]}
        self.assertEqual(self.tool.rings_of(poly), [[[(0, 0), (1, 0), (1, 1), (0, 0)]]])
        self.assertEqual(tuple(self.tool.point_of({'type': 'LineString', 'coordinates': [[0, 0], [1, 1], [2, 2]]})), (1, 1))

    def test_crop_geotiff_builds_local_crop_points_and_tiles(self):
        try:
            import rasterio
            import tifffile
        except ImportError:
            self.skipTest('requires the optional map build dependencies')
        import numpy as np
        from rasterio.transform import from_origin
        from rasterio.warp import transform

        [x], [y] = transform('EPSG:4326', 'EPSG:3005', [-119.55], [49.55])
        data = np.zeros((100, 100), dtype=np.uint8)
        data[10:90, 10:45] = 188   # orchard
        data[10:90, 55:90] = 190   # vineyard
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            geotiff = out / 'annual crop inventory.tif'
            with rasterio.open(geotiff, 'w', driver='GTiff', width=100, height=100, count=1, dtype='uint8',
                               crs='EPSG:3005', transform=from_origin(x - 1500, y + 1500, 30, 30), nodata=0) as ds:
                ds.write(data, 1)
            args = ['builder', 'CA-BC', 'crops', '--crop-geotiff', str(geotiff),
                    '--bbox=-119.59,49.52,-119.51,49.58', '--out', tmp]
            with patch.object(sys, 'argv', args), patch.object(self.tool, 'CACHE', out), \
                    patch('urllib.request.urlopen', side_effect=AssertionError('local crop build used the network')):
                self.tool.main()

            with closing(sqlite3.connect(out / 'crops.sqlite')) as db:
                kinds = {kind for kind, in db.execute('SELECT DISTINCT kind FROM resource')}
                self.assertEqual(kinds, {'orchard', 'vineyard'})
                self.assertEqual(db.execute('SELECT MIN(z), MAX(z) FROM tile').fetchone(), (7, 13))

    def test_microsoft_only_build_skips_statcan_and_credits_the_footprints(self):
        sources = regions.get('CA-BC')['sources']['buildings']
        microsoft = next(s for s in sources if s['publisher'] == 'Microsoft')
        ring = [[-123.11, 49.26], [-123.1099, 49.26], [-123.11, 49.2601], [-123.11, 49.26]]
        feature = {'type': 'Feature', 'geometry': {'type': 'Polygon', 'coordinates': [ring]}}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            archive = out / 'BritishColumbia.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('BritishColumbia.geojson', json.dumps(feature) + '\n')
            args = ['builder', 'CA-BC', 'buildings', '--ms-only', '--out', tmp]
            with patch.object(sys, 'argv', args), patch.object(self.tool, 'CACHE', out), \
                    patch.object(self.tool, 'download', return_value=archive) as download:
                self.tool.main()
                download.assert_called_once_with(microsoft['access'], out / 'msbuildings-ca' / archive.name)
            with closing(sqlite3.connect(out / 'buildings.sqlite')) as db:
                meta = dict(db.execute('SELECT key,value FROM meta'))
                self.assertEqual(meta['buildings'], '1')
                self.assertEqual(meta['source'], 'Microsoft Canadian Building Footprints (ODbL)')
                self.assertEqual(db.execute('SELECT n FROM bldg').fetchall(), [(1,)])

    def test_bad_microsoft_archive_preserves_the_last_build(self):
        region = regions.get('CA-BC')
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            archive = out / 'bad.zip'
            archive.write_bytes(b'incomplete')
            dest = out / 'buildings.sqlite'
            with patch.object(self.tool, 'download', return_value=archive):
                with self.assertRaises(zipfile.BadZipFile):
                    self.tool.build_buildings('CA-BC', region, regions.box('CA-BC'), out, ms_only=True)
                self.assertFalse(dest.exists())
                with closing(sqlite3.connect(dest)) as db, db:
                    db.execute('CREATE TABLE bldg(n INT)')
                    db.execute('INSERT INTO bldg VALUES (9)')
                with self.assertRaises(zipfile.BadZipFile):
                    self.tool.build_buildings('CA-BC', region, regions.box('CA-BC'), out, ms_only=True)
                with closing(sqlite3.connect(dest)) as db:
                    self.assertEqual(db.execute('SELECT n FROM bldg').fetchall(), [(9,)])


if __name__ == '__main__':
    unittest.main()
