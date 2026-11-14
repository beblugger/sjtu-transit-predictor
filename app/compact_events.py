"""Replace redundant visibility geometry with its local linear band models.

Event values and time references retain their original precision. The complete
geometry and audit samples remain available in the full event JSON export.
"""
from __future__ import annotations

import copy
import math

import numpy as np
from shapely.geometry import Point, Polygon, box, shape
from shapely.ops import transform, unary_union


# Permit floating point round trips through the original WGS84 coordinates.
# Events whose band representation differs by more than this retain geometry.
_RECONSTRUCTION_TOLERANCE_M = 1e-5


def _band_polygon(bounds, nx, ny, offset, half_width):
    cell = box(*bounds)
    tangent = (-ny, nx)
    # Long enough to cover this cell independently of the line's absolute offset.
    extent = max(abs(v) for v in bounds) * 4 + abs(offset) * 2 + half_width * 2 + 100
    cx, cy = nx * offset, ny * offset
    vertices = [(cx + t * tangent[0] + n * nx, cy + t * tangent[1] + n * ny)
                for t, n in ((-extent, -half_width), (extent, -half_width),
                             (extent, half_width), (-extent, half_width))]
    return cell.intersection(Polygon(vertices))


def _clip_polygon(bounds, nx, ny, offset):
    extent = max(abs(v) for v in bounds) * 4 + abs(offset) * 2 + 100
    tx, ty = -ny, nx
    cx, cy = nx * offset, ny * offset
    return Polygon([(cx + t*tx - d*nx, cy + t*ty - d*ny)
                    for t, d in ((-extent, 0), (extent, 0), (extent, 2*extent), (-extent, 2*extent))])


def _find_clip(base, actual, bounds):
    # An extra edge inside the uncut band is the horizon half-plane.
    components = [actual] if actual.geom_type == 'Polygon' else list(getattr(actual, 'geoms', []))
    for component in components:
        if component.geom_type != 'Polygon':
            continue
        coordinates = list(component.exterior.coords)
        for a, b in zip(coordinates, coordinates[1:]):
            dx, dy = b[0]-a[0], b[1]-a[1]
            length = math.hypot(dx, dy)
            if length < 0.01 or base.boundary.distance(Point((a[0]+b[0])/2, (a[1]+b[1])/2)) < 0.01:
                continue
            nx, ny = dy/length, -dx/length
            offset = nx*a[0] + ny*a[1]
            inside = actual.representative_point()
            if nx*inside.x + ny*inside.y > offset:
                nx, ny, offset = -nx, -ny, -offset
            if base.intersection(_clip_polygon(bounds, nx, ny, offset)).symmetric_difference(actual).area < 0.1:
                return [nx, ny, offset]
    raise ValueError('Cannot represent the visibility region as bands and a horizon clip')


def compact_visibility(v):
    if v is None:
        return None
    chart = v['chart']
    lon, lat, sx, sy = (chart[k] for k in ('longitude', 'latitude', 'east_m_per_degree', 'north_m_per_degree'))
    def xy(x, y, z=None):
        return ((np.asarray(x)-lon)*sx, (np.asarray(y)-lat)*sy)
    region = transform(xy, shape(v['geometry']))
    domain = transform(xy, shape(v['calculation_domain']))
    rectangle = box(*domain.bounds)
    if (domain.hausdorff_distance(rectangle) > _RECONSTRUCTION_TOLERANCE_M or
            domain.symmetric_difference(rectangle).area > 1e-5):
        raise ValueError('Compact visibility requires a rectangular computation domain')
    bands, rebuilt = [], []
    for model in v['models']:
        ax, ay = model['cross_track_gradient_rad_per_m']
        magnitude = math.hypot(ax, ay)
        if not magnitude:
            raise ValueError('Degenerate centerline')
        nx, ny = ax/magnitude, ay/magnitude
        ox, oy = model['origin_m']
        offset = nx*ox + ny*oy - model['cross_track_at_origin_rad']/magnitude
        half_width = model['contact_radius_rad']/magnitude
        bounds = list(model['bounds_m'])
        band = {'bounds_m': bounds, 'normal': [nx, ny],
                'offset_m': offset, 'half_width_m': half_width,
                'origin_m': list(model['origin_m']),
                'time_offset_s': model['time_offset_s'],
                'time_gradient_s_per_m': list(model['time_gradient_s_per_m'])}
        base = _band_polygon(bounds, nx, ny, offset, half_width).intersection(domain)
        actual = region.intersection(box(*model['bounds_m'])).intersection(domain)
        if base.symmetric_difference(actual).area > 0.1:
            band['clip'] = _find_clip(base, actual, bounds)
        if 'clip' in band:
            base = base.intersection(_clip_polygon(bounds, *band['clip']))
        rebuilt.append(base)
        bands.append(band)
    if not rebuilt or unary_union(rebuilt).hausdorff_distance(region) > _RECONSTRUCTION_TOLERANCE_M:
        raise ValueError('Centerline bands do not reproduce the original visibility region')
    omitted = {'geometry', 'boundaries', 'calculation_domain', 'campus_geometry',
               'centerline', 'models', 'boundary_samples'}
    result = {key: copy.deepcopy(value) for key, value in v.items() if key not in omitted}
    result.update(domain_m=list(domain.bounds), bands=bands)
    return result


def compact_snapshot(payload):
    """Preserve event fields and compact representable visibility geometry only."""
    result = copy.deepcopy(payload)
    events = result.get('events')
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise ValueError('events must be a list of objects')
    for event in events:
        visibility = event.get('visibility')
        if visibility:
            try:
                event['visibility'] = compact_visibility(visibility)
            except ValueError:
                # A nonrectangular domain or geometry that cannot be represented
                # by one band/clip per model keeps the full original geometry.
                # A presentation optimization must not discard a valid snapshot.
                pass
    return result
