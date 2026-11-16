import pytest

from scripts.validate_export import BASE_TOLERANCE_ARCSEC, separation_tolerance_arcsec


class _Distance:
    def __init__(self, km):
        self.km = km


class _Position:
    def __init__(self, km):
        self._km = km

    def distance(self):
        return _Distance(self._km)


class _Relative:
    def __init__(self, km):
        self._km = km

    def at(self, t):
        return _Position(self._km)


class _Satellite:
    def __init__(self, geocentric_km, topocentric_km):
        self.geocentric_km = geocentric_km
        self.topocentric_km = topocentric_km

    def at(self, t):
        return _Position(self.geocentric_km)

    def __sub__(self, site):
        return _Relative(self.topocentric_km)


def test_frame_tolerance_grows_for_close_topocentric_passes():
    overhead = separation_tolerance_arcsec(_Satellite(6_900, 88_000), None, None)
    near = separation_tolerance_arcsec(_Satellite(6_900, 250), None, None)
    assert BASE_TOLERANCE_ARCSEC < overhead < BASE_TOLERANCE_ARCSEC + 0.02
    # A close LEO pass magnifies the fixed frame offset by the range ratio.
    assert near == pytest.approx(BASE_TOLERANCE_ARCSEC + 0.1 * (6_900 / 250))
    assert 1.7 < near < 5
