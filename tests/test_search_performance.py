"""Guard reductions in expensive work without timing-dependent assertions."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from app.astronomy import SatellitePosition, TargetPosition, unit_vector_rows
from app.config import Settings
from app.geo import unit_from_az_alt
from app.models import Target
from app.runtime import build_runtime
from app.satellites import CandidateWindow, ClosestApproach, RefinedConjunction


START = datetime(2026, 11, 5, tzinfo=timezone.utc)


@pytest.fixture
def runtime(tmp_path):
    runtime = build_runtime(Settings(database_path=tmp_path / 'cache.db',
                                     ephemeris_path=tmp_path / 'missing.bsp'))
    yield runtime
    runtime.store.close()


@pytest.mark.parametrize('altitudes, expected_targets', [
    ((45., 45.), [Target.SUN, Target.MOON]),
    ((-10., 45.), [Target.MOON]),
    ((45., -10.), [Target.SUN]),
    ((-10., -10.), []),
    ((-0.49, -0.5), [Target.SUN]),
])
def test_propagation_shared_by_visible_targets(runtime, monkeypatch, altitudes, expected_targets):
    service = runtime.satellites
    propagations, searched = [], []
    samples = (np.zeros((3, 3)), np.ones(3))
    record = SimpleNamespace(satrec=object(), entry=SimpleNamespace(norad_id=1))

    def targets(target, observer, times):
        altitude = altitudes[0 if target == 'sun' else 1]
        return tuple(np.full(len(times), value) for value in (180., altitude, 1e11, 900.))

    def propagate(satrec, observer, times):
        propagations.append(satrec)
        return samples

    def candidates(record, target, *args, satellite_samples):
        assert satellite_samples is samples
        searched.append(target)
        return []

    monkeypatch.setattr(runtime.astronomy, 'targets_at', targets)
    monkeypatch.setattr(runtime.astronomy, 'satellite_vectors', propagate)
    monkeypatch.setattr(service, '_candidate_windows', candidates)
    assert service._compute_events(START, START + timedelta(minutes=2), START, [record]) == []
    assert searched == expected_targets
    assert len(propagations) == bool(expected_targets)


def test_reused_propagation_preserves_candidates_and_invalid_samples(runtime, monkeypatch):
    service = runtime.satellites
    times = [START + timedelta(seconds=60 * index) for index in range(4)]
    vectors = unit_vector_rows(np.array([179., 180., 181., 182.]), np.full(4, 45.))
    vectors[-1] = np.nan  # A failed sample must not remove the valid preceding arc.
    samples = (vectors, np.array([500000., 500000., 500000., np.nan]))
    monkeypatch.setattr(runtime.astronomy, 'satellite_vectors', lambda *args: samples)
    record = SimpleNamespace(satrec=object(), size=None)
    args = (record, Target.SUN, runtime.campus.center, times,
            unit_vector_rows(np.full(4, 180.), np.full(4, 45.)),
            np.full(4, 45.), np.full(4, 900.))
    original = service._candidate_windows(*args)
    reused = service._candidate_windows(*args, satellite_samples=samples)
    assert original == reused
    assert len(reused) == 2
    assert np.isnan(vectors[-1]).all()


def test_ranking_long_transit_omits_contacts_but_final_refinement_keeps_them(runtime, monkeypatch):
    calls = []

    def target_at(target, observer, value):
        calls.append((value - START).total_seconds())
        return TargetPosition(180., 45., 1e11, 100.)

    def satellite_position(satrec, observer, value):
        offset = (value - START).total_seconds()
        altitude = 45. + (offset - 2.) / 36000  # 0.1 arcsec/s, ~2000 second transit.
        return SatellitePosition(180., altitude, 500000., 0., 0., 0.,
                                 unit_from_az_alt(180., altitude))

    monkeypatch.setattr(runtime.astronomy, 'target_at', target_at)
    monkeypatch.setattr(runtime.astronomy, 'satellite_position', satellite_position)
    service = runtime.satellites
    record = SimpleNamespace(satrec=object(), size=None)
    args = (record, Target.SUN, service._grid[0], START)
    minimum = service._refine_at_observer(*args, compute_contacts=False)
    assert type(minimum) is ClosestApproach
    assert max(abs(offset) for offset in calls) <= 60
    ranking_calls = len(calls)
    calls.clear()
    final = service._refine_at_observer(*args)
    assert isinstance(final, RefinedConjunction)
    assert final.minimum_time == minimum.minimum_time
    assert final.minimum_separation_arcsec == minimum.minimum_separation_arcsec
    assert (final.end_time - final.start_time).total_seconds() == pytest.approx(2000., abs=.01)
    assert final.start_time < final.minimum_time < final.end_time
    assert len(calls) > ranking_calls
    assert max(abs(offset) for offset in calls) > 60

    # Exercise the actual grid-selection path: only the final observer may
    # request contacts, and that final solve must use exact target positions.
    refinement_calls = []
    refine = service._refine_at_observer

    def counted(*args, **kwargs):
        refinement_calls.append(kwargs)
        return refine(*args, **kwargs)

    monkeypatch.setattr(service, '_target_interpolator',
                        lambda target, center: lambda observer, value: target_at(target, observer, value))
    monkeypatch.setattr(service, '_refine_at_observer', counted)
    monkeypatch.setattr(service, '_build_event', lambda record, target, best, now: best)
    selected = service._select_and_refine(record, Target.SUN, CandidateWindow(Target.SUN, START), START)
    assert isinstance(selected, RefinedConjunction)
    assert len(refinement_calls) > 20
    assert all(call['compute_contacts'] is False for call in refinement_calls[:-1])
    assert refinement_calls[-1] == {}
