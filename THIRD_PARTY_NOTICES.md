# Stellarium satellite photometry

The brightness models are documented in Stellarium's Satellites plugin,
`Satellite::getVMagnitude` and `calculateIlluminatedFraction`:

- [Pinned model reference](https://github.com/Stellarium/stellarium/blob/60e6060f843dd6b373cee5c14631969a9b5f1647/plugins/Satellites/src/Satellite.cpp)
- [Pinned satellite catalog](https://github.com/Stellarium/stellarium/blob/60e6060f843dd6b373cee5c14631969a9b5f1647/plugins/Satellites/resources/satellites.json)

`app/photometry.py` expresses those mathematical models in Python:

- Starlink: magnitude 5.93 at 1000 km, scaled with distance; the upstream
  DarkSat factor is retained.
- Standard magnitude: the McCants distance and illuminated fraction formula,
  including the upstream minimum fraction of 10⁻⁶.
- Radar cross-section: the diffuse sphere model with the upstream phase dependent
  albedo polynomials (DOI: [10.1117/12.2014623](https://doi.org/10.1117/12.2014623)).

The implementation does not include Iridium flares, atmospheric extinction,
or the optional penumbral magnitude offset. It uses the actual
Sun–satellite–observer phase angle and Skyfield's independent binary sunlight
test. Missing parameters remain missing; Stellarium's display placeholders
(7 or 17 mag) are not treated as physical predictions.

`app/data/satellite_photometry.json` is a reduced, renamed extract of the
catalog's factual `stdMag` and `rcs` fields, keyed by NORAD ID. The original
catalog's revision, source SHA256, retrieval date and source URLs are embedded
in the extract. No TLEs or display preferences were imported. RCS supplies the approximate photometric model and, when no documented physical
span exists, the Stellarium equivalent circular diameter D = 2√(RCS/π). Known
physical spans take priority; ISS retains its documented 109 m maximum span.
The angular radius is atan(D/(2r)), angular diameter is twice that value.
RCS equivalents are explicitly marked as estimates with low confidence and
are stored separately from physical spans. The same radius is used in candidate
screening, contact times, inverse regions and independent export validation.
Stellarium’s 0.1 arcsecond display default is not used as a measured size;
objects lacking both inputs retain unknown size and the center crossing criterion.

Stellarium is copyright its contributors; the model source identifies
Copyright © 2009, 2012 Matthew Gates. The upstream source and extracted data
are provided under GPL-2.0-or-later. The upstream license text is included at
[licenses/Stellarium-GPL-2.0.txt](licenses/Stellarium-GPL-2.0.txt).
