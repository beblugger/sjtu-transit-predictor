"""Convert the public SJTU building-outline response into a Minhang map asset.

Usage: python scripts/extract_campus_buildings.py response.json output.geojson
This script reads a previously downloaded snapshot; it never crawls map tiles.
"""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from shapely import wkt
from shapely.geometry import box, mapping

source, target = map(Path, sys.argv[1:])
raw = source.read_bytes()
# The source map's Minhang extent plus Nanyang Beiyuan; not the prediction domain.
extent = box(121.415, 31.015, 121.45, 31.04)
features = []
for item in json.loads(raw)['list']:
    polygon = wkt.loads(item['polygon'])
    if not extent.covers(polygon.representative_point()):
        continue
    if polygon.geom_type not in ('Polygon', 'MultiPolygon') or not polygon.is_valid:
        raise ValueError('Invalid source building polygon')
    features.append({'type': 'Feature', 'properties': {}, 'geometry': mapping(polygon)})
if not features:
    raise ValueError('No Minhang buildings found')
payload = {'type': 'FeatureCollection', 'provenance': {
    'source': 'https://map.sjtu.edu.cn/',
    'endpoint': 'https://map.sjtu.edu.cn/jmap/getOutlineOfBuilding',
    'retrieved_at': datetime.now(timezone.utc).isoformat(),
    'source_sha256': hashlib.sha256(raw).hexdigest(),
    'coordinate_basis': 'Source longitude/latitude retained; campus WMS advertises CRS:84 / EPSG:3857. No GCJ-02 conversion.',
    'coordinate_accuracy': 'Outline endpoint does not declare a datum or survey accuracy; WMS correspondence is not a survey validation.',
    'rights': 'SJTU electronic map source; no redistribution licence stated in the response. Kept for local review; not published.',
}, 'features': features}
target.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n')
print(f'{len(features)} buildings, {target.stat().st_size} bytes')
