"""Invert local angular alignment into a ground transit region.

For angular residual g(p,t), linearize g = g0 + J dp + v dt.
Eliminating time gives |n.(g0 + J dp)| <= apparent contact radius,
where n is perpendicular to v. Adaptively split the campus chart until
sampled nonlinear contact residuals satisfy the requested spatial tolerance.
These residuals measure numerical approximation, not orbital uncertainty.
"""
from __future__ import annotations

from datetime import timedelta
import math

import numpy as np
from scipy.optimize import minimize_scalar
from shapely.geometry import Polygon, MultiPolygon, LineString, MultiLineString, Point, box, mapping
from shapely.ops import unary_union
from shapely import transform

from app.geo import Geodetic, WGS84_A, WGS84_E2, enu_rotation, unit_from_az_alt


class FootprintSolver:
    def __init__(self, astronomy, campus, record, event, *, tolerance_m=5.0, max_depth=6, exterior_margin_m=1500.0):
        self.astronomy, self.record, self.event = astronomy, record, event
        self.tolerance, self.max_depth = tolerance_m, max_depth
        self.lon0, self.lat0 = campus.center.longitude_deg, campus.center.latitude_deg
        lat = math.radians(self.lat0)
        w = math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
        self.east_scale = WGS84_A / w * math.cos(lat) * math.pi / 180
        self.north_scale = WGS84_A * (1 - WGS84_E2) / w ** 3 * math.pi / 180
        self.campus = unary_union([Polygon([self.xy(lon, lat) for lon, lat in ring]) for ring in campus.rings])
        xmin, ymin, xmax, ymax = self.campus.bounds
        width, height = xmax - xmin + 2 * exterior_margin_m, ymax - ymin + 2 * exterior_margin_m
        width = max(width, height * 960 / 560)
        self.domain = (box((xmin + xmax - width) / 2, (ymin + ymax - height) / 2,
                           (xmin + xmax + width) / 2, (ymin + ymax + height) / 2)
                       if exterior_margin_m else self.campus)
        reference = Geodetic(self.lat0, self.lon0, event.observer.elevation_m)
        target = astronomy.target_at(event.target.value, reference, event.minimum_time)
        u = enu_rotation(self.lat0, self.lon0).T @ unit_from_az_alt(target.azimuth_deg, target.altitude_deg)
        axis = np.array([0., 0., 1.]) if abs(u[2]) < .9 else np.array([1., 0., 0.])
        self.e1 = np.cross(u, axis); self.e1 /= np.linalg.norm(self.e1)
        self.e2 = np.cross(u, self.e1)
        self.models, self.areas, self.lines = [], [], []
        self.contact_lines = []
        self.sampled_error, self.time_error = 0., 0.
        self.contact_samples = 0
        self.boundary_samples = []
        self.half_window = max(3., min(600., event.duration_ms / 1000 * 1.5 + 3.))

    def xy(self, lon, lat):
        return ((lon - self.lon0) * self.east_scale, (lat - self.lat0) * self.north_scale)

    def observer(self, p):
        return Geodetic(self.lat0 + p[1] / self.north_scale,
                        self.lon0 + p[0] / self.east_scale, self.event.observer.elevation_m)

    def sample(self, p, offset):
        observer = self.observer(p)
        time = self.event.minimum_time + timedelta(seconds=float(offset))
        target = self.astronomy.target_at(self.event.target.value, observer, time)
        sat = self.astronomy.satellite_position(self.record.satrec, observer, time)
        rotation = enu_rotation(observer.latitude_deg, observer.longitude_deg).T
        us, ut = rotation @ sat.vector_enu, rotation @ unit_from_az_alt(target.azimuth_deg, target.altitude_deg)
        cosine = float(np.clip(us @ ut, -1., 1.))
        angle = math.atan2(float(np.linalg.norm(np.cross(us, ut))), cosine)
        tangent = us - cosine * ut
        vector = np.array([tangent @ self.e1, tangent @ self.e2])
        vector *= angle / max(float(np.linalg.norm(vector)), 1e-30)
        radius = target.angular_radius_arcsec
        if self.record.size:
            radius += self.record.size.angular_diameter_arcsec(sat.range_m) / 2
        return vector, math.radians(radius / 3600), min(target.altitude_deg, sat.altitude_deg)

    def closest(self, p, guess):
        width = self.half_window
        for _ in range(5):
            result = minimize_scalar(lambda dt: float(np.linalg.norm(self.sample(p, guess + dt)[0]) ** 2),
                                     bounds=(-width, width), method='bounded', options={'xatol': 1e-5})
            if abs(result.x) < width * .98:
                offset = guess + float(result.x)
                return offset, self.sample(p, offset)
            width *= 3
        raise RuntimeError('Could not bracket local transit minimum for footprint')

    @staticmethod
    def halfplane(vertices, a, limit):
        output = []
        for p, q in zip(vertices, vertices[1:] + vertices[:1]):
            fp, fq = float(a @ p - limit), float(a @ q - limit)
            if fp <= 0:
                output.append(p)
            if (fp <= 0) != (fq <= 0):
                output.append(p + (q - p) * fp / (fp - fq))
        return output

    def line(self, p, a, c, level, extent):
        normal = a / np.linalg.norm(a)
        center = p + (level - c) * a / float(a @ a)
        tangent = np.array([-normal[1], normal[0]])
        return LineString([center - extent * tangent, center + extent * tangent])

    def solve_tile(self, bounds, guess, depth):
        cell = self.domain.intersection(box(*bounds))
        if cell.is_empty or cell.area < .001:
            return
        xmin, ymin, xmax, ymax = bounds
        p = np.array([(xmin + xmax) / 2, (ymin + ymax) / 2])
        t, (g, radius, altitude) = self.closest(p, guess)
        step = 20.
        jac = np.column_stack([(self.sample(p + d, t)[0] - self.sample(p - d, t)[0]) / (2 * step)
                               for d in (np.array([step, 0]), np.array([0, step]))])
        velocity = (self.sample(p, t + .05)[0] - self.sample(p, t - .05)[0]) / .1
        speed = float(np.linalg.norm(velocity))
        if speed < 1e-12:
            raise RuntimeError('Degenerate temporal derivative in footprint inversion')
        normal = np.array([-velocity[1], velocity[0]]) / speed
        a, c = normal @ jac, float(normal @ g)
        gradient = float(np.linalg.norm(a))
        if gradient < 1e-14:
            raise RuntimeError('Degenerate spatial derivative in footprint inversion')
        time_gradient = -(velocity @ jac) / speed ** 2
        predicted = lambda q: t + float(time_gradient @ (q - p))
        alt_gradient = np.array([(self.sample(p + d, predicted(p + d))[2] - self.sample(p - d, predicted(p - d))[2]) / (2 * step)
                                 for d in (np.array([step, 0]), np.array([0, step]))])
        vertices = [np.array([xmin, ymin]), np.array([xmax, ymin]), np.array([xmax, ymax]), np.array([xmin, ymax])]
        for direction, limit in ((a, radius - c + a @ p), (-a, radius + c - a @ p),
                                 (-alt_gradient, altitude - alt_gradient @ p)):
            vertices = self.halfplane(vertices, direction, limit)
            if len(vertices) < 3:
                break
        area = cell.intersection(Polygon(vertices)) if len(vertices) >= 3 else Polygon()
        extent = math.hypot(xmax - xmin, ymax - ymin) * 3 + 1
        checks = [(np.array([x, y]), False) for x in (xmin, p[0], xmax) for y in (ymin, p[1], ymax)
                  if cell.buffer(.01).covers(Point(x, y))]
        seed = np.array(self.xy(self.event.observer.longitude, self.event.observer.latitude))
        seed_in_cell = cell.buffer(1e-6).covers(Point(seed))
        if seed_in_cell:
            checks.append((seed, False))
        # Rectangular corners can lie outside an oblique campus. Include the
        # actual component hull vertices and edge midpoints, especially when
        # the model says that the entire campus lies inside the transit band.
        for component in components(cell, 'Polygon'):
            vertices = list(component.convex_hull.exterior.coords)
            for left, right in zip(vertices, vertices[1:]):
                checks.append((np.array(left), False))
                middle = (np.array(left) + np.array(right)) / 2
                if cell.buffer(.01).covers(Point(middle)):
                    checks.append((middle, False))
        contacts = []
        for level in (-radius, radius):
            edge = cell.intersection(self.line(p, a, c, level, extent))
            for segment in components(edge, 'LineString'):
                for fraction in (0., .5, 1.):
                    point = segment.interpolate(fraction, normalized=True)
                    contacts.append(np.array(point.coords[0]))
        checks.extend((q, True) for q in contacts)
        errors, time_errors, accepted_contacts, fail = [], [], [], bool(area.is_empty and seed_in_cell)
        for q, contact in checks:
            exact_t, (exact_g, exact_radius, exact_alt) = self.closest(q, predicted(q))
            margin = float(np.linalg.norm(exact_g)) - exact_radius
            linear_margin = abs(float(a @ (q - p)) + c) - radius
            if contact and exact_alt > 0:
                accepted_contacts.append(q)
                error = abs(margin) / gradient
                errors.append(error)
                fail |= error > self.tolerance
            if (margin < 0 and exact_alt > 0) != (linear_margin < 0 and altitude + alt_gradient @ (q - p) > 0):
                fail |= abs(margin) / gradient > self.tolerance
            if margin < 0 or linear_margin < 0:
                time_errors.append(abs(exact_t - predicted(q)))
        if fail:
            if depth >= self.max_depth:
                raise RuntimeError(f'Footprint approximation failed {self.tolerance} m sampled tolerance')
            for child in ((xmin, ymin, p[0], p[1]), (p[0], ymin, xmax, p[1]),
                          (xmin, p[1], p[0], ymax), (p[0], p[1], xmax, ymax)):
                center = np.array([(child[0] + child[2]) / 2, (child[1] + child[3]) / 2])
                self.solve_tile(child, predicted(center), depth + 1)
            return
        if area.is_empty or area.area < .001:
            return
        self.sampled_error = max(self.sampled_error, max(errors, default=0.))
        self.time_error = max(self.time_error, max(time_errors, default=0.))
        self.contact_samples += len(errors)
        for q in accepted_contacts:
            self.boundary_samples.append({'longitude': self.lon0 + float(q[0]) / self.east_scale,
                                          'latitude': self.lat0 + float(q[1]) / self.north_scale,
                                          'predicted_minimum_time': (self.event.minimum_time + timedelta(seconds=predicted(q))).isoformat(),
                                          'normal_gradient_rad_per_m': gradient})
        self.areas.append(area)
        for level in (-radius, radius):
            self.contact_lines.extend(components(area.buffer(1e-6).intersection(self.line(p, a, c, level, extent)), 'LineString'))
        self.lines.extend(components(area.intersection(self.line(p, a, c, 0, extent)), 'LineString'))
        self.models.append({'bounds_m': list(bounds), 'origin_m': p.tolist(),
                            'time_offset_s': t, 'time_gradient_s_per_m': time_gradient.tolist(),
                            'cross_track_at_origin_rad': c, 'cross_track_gradient_rad_per_m': a.tolist(),
                            'contact_radius_rad': radius})

    def solve(self):
        self.solve_tile(self.domain.bounds, 0., 0)
        if not self.areas:
            raise RuntimeError('Transit event has no inverse ground region')
        region = unary_union(self.areas)
        polygons = components(region, 'Polygon')
        region = MultiPolygon(polygons)
        lines = MultiLineString(self.lines) if self.lines else MultiLineString([])
        boundaries = unary_union(self.contact_lines) if self.contact_lines else MultiLineString([])
        convert = lambda x, y, z=None: (self.lon0 + np.asarray(x) / self.east_scale,
                                       self.lat0 + np.asarray(y) / self.north_scale)
        return {'method': 'adaptive_first_order_angular_inverse', 'coordinate_system': 'WGS84',
                'geometry': mapping(transform(region, convert, interleaved=False)),
                'boundaries': mapping(transform(boundaries, convert, interleaved=False)),
                'calculation_domain': mapping(transform(self.domain, convert, interleaved=False)),
                'campus_geometry': mapping(transform(region.intersection(self.campus), convert, interleaved=False)),
                'campus_area_m2': float(region.intersection(self.campus).area),
                'centerline': mapping(transform(lines, convert, interleaved=False)), 'area_m2': float(region.area),
                'reference_time': self.event.minimum_time.isoformat(),
                'chart': {'longitude': self.lon0, 'latitude': self.lat0,
                          'east_m_per_degree': self.east_scale, 'north_m_per_degree': self.north_scale,
                          'observer_elevation_m': self.event.observer.elevation_m},
                'models': self.models, 'sampled_boundary_tolerance_m': self.tolerance,
                'maximum_sampled_boundary_error_m': self.sampled_error if self.contact_samples else None,
                'maximum_sampled_time_error_s': self.time_error, 'contact_validation_count': self.contact_samples,
                'boundary_samples': self.boundary_samples,
                'orbital_uncertainty_m': None}


def components(geometry, kind):
    if geometry.is_empty:
        return []
    if geometry.geom_type == kind:
        return [geometry]
    return [part for item in getattr(geometry, 'geoms', []) for part in components(item, kind)]
