from datetime import datetime, timezone
from types import SimpleNamespace
import math

import numpy as np
import pytest
from shapely.geometry import shape, Point

from app.campus import Campus
from app.footprints import FootprintSolver
from app.geo import Geodetic, unit_from_az_alt, WGS84_A, WGS84_E2
from app.astronomy import TargetPosition, SatellitePosition
from app.models import Target, ObserverPoint


class LinearSky:
    """Known inverse: y=0 centerline and |y| <= radius/k transit strip."""
    def __init__(self, time, lat=0, lon=0, curvature=0):
        self.time, self.curvature = time, curvature
        self.lat, self.lon = lat, lon
        self.north_scale = WGS84_A * (1 - WGS84_E2) * math.pi / 180
        self.east_scale = WGS84_A * math.pi / 180

    def target_at(self, target, observer, time):
        return TargetPosition(0, 45, 1e11, 100.)

    def satellite_position(self, satrec, observer, time):
        x = (observer.longitude_deg-self.lon)*self.east_scale
        y = (observer.latitude_deg-self.lat)*self.north_scale
        t = (time-self.time).total_seconds()
        # Cross track angle changes by 1 arcsec/m, along track 100 arcsec/s.
        altitude = 45 + (y + self.curvature*x*x)/3600
        azimuth = (t*100-x)/3600/math.cos(math.pi/4)
        return SatellitePosition(azimuth, altitude, 500000, 0, 0, 0, unit_from_az_alt(azimuth, altitude))


def make_case(curvature=0):
    time = datetime(2026,11,4,tzinfo=timezone.utc)
    astronomy = LinearSky(time, curvature=curvature)
    scale_x, scale_y = astronomy.east_scale, astronomy.north_scale
    ring=tuple((x/scale_x,y/scale_y) for x,y in [(-500,-500),(500,-500),(500,500),(-500,500),(-500,-500)])
    campus=Campus('square',(ring,),(-.01,-.01,.01,.01),Geodetic(0,0))
    event=SimpleNamespace(target=Target.SUN,minimum_time=time,duration_ms=2000,observer=ObserverPoint(latitude=0,longitude=0))
    record=SimpleNamespace(satrec=object(),size=None)
    return FootprintSolver(astronomy,campus,record,event,tolerance_m=2)


def test_inverse_strip_has_physical_width_and_position_dependent_time():
    result=make_case().solve()
    # Width 200m x length 1000m, rather than a circle around an observer.
    assert result['area_m2']==pytest.approx(200000,rel=.015)
    assert len(result['models'])==1
    assert result['contact_validation_count']>=6
    model=result['models'][0]
    assert model['time_gradient_s_per_m'][0]==pytest.approx(.01,abs=.0001)
    assert result['maximum_sampled_boundary_error_m']<=2
    assert result['orbital_uncertainty_m'] is None
    geometry=shape(result['geometry'])
    assert geometry.covers(Point(0,0))
    assert not geometry.covers(Point(0,.003))


def test_nonlinear_boundary_triggers_adaptive_piecewise_inversion():
    result=make_case(curvature=.0004).solve()
    assert len(result['models'])>1
    assert result['maximum_sampled_boundary_error_m']<=2
    assert result['area_m2']==pytest.approx(200000,rel=.04)


def test_unconverged_inverse_is_not_published_as_a_valid_region():
    solver=make_case(curvature=.0004)
    solver.max_depth=0
    with pytest.raises(RuntimeError,match='approximation failed'):
        solver.solve()
