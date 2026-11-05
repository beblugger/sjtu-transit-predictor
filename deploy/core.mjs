import {weatherCondition} from './presentation.mjs';
export const PAGE_SIZE = 10;
export const TARGETS = {sun: '太阳', moon: '月球'};
export const WEATHER = {announced: '模型评分达标', suppressed: '模型评分未达标', unavailable: '天气不可用'};
export const REASONS = {
  object_size_unknown_centerline_only: '卫星尺寸未知，计算采用卫星中心穿过日/月视圆盘的条件',
  object_size_rcs_estimate: '卫星尺寸采用 RCS 等效圆近似，角半径、接触时刻和可见区据此估算；不代表真实光学轮廓',
  orbit_epoch_old: '轨道历元距本次生成时间超过六小时',
  long_orbit_extrapolation: '事件时刻距轨道历元超过 24 小时',
  weather_window_not_covered: '天气数据未覆盖完整评分窗口',
  weather_unavailable: '缺少天气数据', weather_stale: '天气缓存已过期',
  ensemble_unavailable: '缺少集合预报', ensemble_stale: '集合预报已过期',
  deterministic_weather_stale: '确定性天气缓存已过期',
  ensemble_weather_stale_or_unavailable: '集合预报已过期或不可用',
  near_term_observed_overcast_or_precipitation: '近期确定性天气提示阴天或降水',
  weather_probability_invalid: '天气统计指标无效',
  composite_score_below_horizon_threshold: '综合评分低于该提前量的阈值',
  model_disagreement: '天气模型之间分歧较大', forecast_run_unstable: '相邻预报运行结果变化较大',
};
export const present = value => typeof value === 'number' && Number.isFinite(value);
export function number(value, digits = 2) { return present(value) ? value.toFixed(digits) : '未知'; }
export function formatTime(value, zone, milliseconds = false) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '未知';
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
    ...(milliseconds ? {fractionalSecondDigits: 3} : {}),
  }).formatToParts(date);
  const p = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}${milliseconds ? '.' + p.fractionalSecond : ''}`;
}
export const zoneName = zone => zone === 'UTC' ? 'UTC' : 'CST';
export const inputTime = (value, zone) => formatTime(value, zone).replace(' ', 'T');
export function inputInstant(value, zone) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(value)) return NaN;
  return Date.parse(value + (zone === 'UTC' ? 'Z' : '+08:00'));
}
export const eventKey = event => [event.identifier, event.target, event.minimum_time,
  event.observer.latitude, event.observer.longitude].join('|');
export function distanceMeters(a, b) {
  if (!a || !b) return null;
  const ecef = p => {
    const lat = p.latitude * Math.PI / 180, lon = p.longitude * Math.PI / 180;
    const e2 = 0.0066943799901413165, n = 6378137 / Math.sqrt(1 - e2 * Math.sin(lat) ** 2);
    return [n * Math.cos(lat) * Math.cos(lon), n * Math.cos(lat) * Math.sin(lon), n * (1 - e2) * Math.sin(lat)];
  };
  const p = ecef(a), q = ecef(b);
  // Surface-height-zero ECEF chord, adequate for campus distances; not walking distance.
  return Math.hypot(...p.map((v, i) => v - q[i]));
}
export function filterEvents(events, filters, location = null, weather = null, now = Date.now()) {
  const needle = filters.search.trim().toLowerCase();
  // Silently exclude events whose minimum has already occurred. Callers may pass a
  // fixed instant in tests; the UI passes the current time on every render.
  const futureOnly = Number.isFinite(now);
  const result = events.filter(event => {
    const t = Date.parse(event.minimum_time), diameter = event.object_angular_diameter_arcsec;
    const distance = location && present(filters.maxDistance) ? visibilityAt(event, location)?.distance_m : null;
    return (!location || !present(filters.maxDistance) || (present(distance) && distance <= filters.maxDistance)) &&
      t >= filters.from && t < filters.to && (!futureOnly || t > now) &&
      (filters.target === 'all' || filters.target === event.target) &&
      (!needle || `${event.name} ${event.identifier}`.toLowerCase().includes(needle)) &&
      event.altitude_deg >= filters.minAltitude &&
      (present(diameter) ? diameter >= filters.minDiameter : filters.keepUnknown) &&
      (filters.weather === 'all' || (['clear','overcast'].includes(filters.weather) ? filters.weather === weatherCondition(weather,event.minimum_time).kind : filters.weather === event.weather.decision)) &&
      (!filters.insideOnly || !location || visibilityAt(event, location)?.inside === true);
  });
  result.sort((a, b) => {
    let difference = 0;
    if (filters.sort === 'altitude') difference = b.altitude_deg - a.altitude_deg;
    if (filters.sort === 'diameter') difference = (b.object_angular_diameter_arcsec ?? -1) - (a.object_angular_diameter_arcsec ?? -1);
    if (filters.sort === 'distance' && location) difference = (visibilityAt(a, location)?.distance_m ?? Infinity) - (visibilityAt(b, location)?.distance_m ?? Infinity);
    return difference || Date.parse(a.minimum_time) - Date.parse(b.minimum_time);
  });
  return result;
}
export function timeOpacity(time, now = Date.now()) {
  const hours = (Date.parse(time) - now) / 3600000;
  return hours <= 6 ? 1 : hours <= 24 ? .65 : hours <= 48 ? .4 : .2;
}
export function validateSnapshot(manifest, payload, campus) {
  if (manifest.schema_version !== 1) throw new Error('不支持的数据版本，请更新页面');
  const from = Date.parse(manifest.from_time), to = Date.parse(manifest.to_time);
  if (!Number.isFinite(from) || !Number.isFinite(to) || from >= to || !Number.isFinite(Date.parse(manifest.generated_at))) throw new Error('数据覆盖或生成时间无效');
  if (payload.catalog_sha256 !== manifest.catalog_sha256 || payload.generated_at !== manifest.generated_at) throw new Error('数据版本不一致');
  if (!Array.isArray(payload.events) || payload.events.length !== manifest.event_count) throw new Error('事件数量与版本清单不一致');
  if (campus.type !== 'FeatureCollection' || !campus.features?.length) throw new Error('校区 GeoJSON 无效');
  for (const event of payload.events) {
    const start = Date.parse(event.start_time), minimum = Date.parse(event.minimum_time), end = Date.parse(event.end_time);
    if (!TARGETS[event.target] || !event.observer || !present(event.observer.latitude) || !present(event.observer.longitude) ||
        Math.abs(event.observer.latitude) > 90 || Math.abs(event.observer.longitude) > 180 ||
        ![start, minimum, end].every(Number.isFinite) || start > minimum || minimum > end ||
        minimum < from || minimum >= to || !WEATHER[event.weather?.decision] ||
        !present(event.altitude_deg) || !present(event.duration_ms) || event.duration_ms < 0 ||
        !present(event.range_km) || event.range_km <= 0) {
      throw new Error('事件存在缺失或无效的关键字段');
    }
  }
}
function csvCell(value) {
  let text = value == null ? '' : String(value);
  if (typeof value === 'string' && /^[=+@\-\t\r]/.test(text)) text = "'" + text;
  return '"' + text.replaceAll('"', '""') + '"';
}
export function eventsCSV(events) {
  const fields = ['norad_id', 'name', 'target', 'start_time_utc', 'minimum_time_utc', 'end_time_utc',
    'duration_s', 'altitude_deg', 'azimuth_deg', 'range_km', 'angular_diameter_arcsec', 'maximum_size_m',
    'observer_latitude_deg_wgs84', 'observer_longitude_deg_wgs84', 'observer_elevation_m',
    'orbit_epoch_utc', 'orbit_age_at_event_hours', 'weather_decision', 'weather_score', 'reason_codes', 'campus_visibility_area_m2', 'sampled_boundary_error_m', 'visibility_area_m2',
    'angular_radius_arcsec','angular_diameter_method','rcs_equivalent_diameter_m','radar_cross_section_m2','angular_size_source','angular_size_confidence'];
  const rows = events.map(e => [e.identifier, e.name, e.target, e.start_time, e.minimum_time, e.end_time,
    e.duration_ms / 1000, e.altitude_deg, e.azimuth_deg, e.range_km, e.object_angular_diameter_arcsec, e.physical_size_m,
    e.observer.latitude, e.observer.longitude, e.observer.elevation_m, e.orbit_epoch,
    e.orbit_age_at_event_hours, e.weather.decision, e.weather.score, e.reason_codes.join(';'), e.visibility?.campus_area_m2 ?? e.visibility?.area_m2, e.visibility?.maximum_sampled_boundary_error_m, e.visibility?.area_m2,
    e.object_angular_radius_arcsec,e.angular_diameter_method,e.equivalent_diameter_m,e.satellite_rcs_m2,e.angular_size_source,e.angular_size_confidence]);
  return '\ufeff' + [fields, ...rows].map(row => row.map(csvCell).join(',')).join('\r\n') + '\r\n';
}

function ringPosition(point, ring) {
  const [x, y] = point;
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [ax, ay] = ring[j], [bx, by] = ring[i];
    const dx = bx - ax, dy = by - ay;
    const u = Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy || 1)));
    if (Math.hypot(x - ax - u * dx, y - ay - u * dy) < 1e-10) return {inside: true, boundary: true};
    if ((ay > y) !== (by > y) && x < (bx - ax) * (y - ay) / (by - ay) + ax) inside = !inside;
  }
  return {inside, boundary: false};
}
export function regionContains(geometry, point) {
  const polygons = geometry.type === 'Polygon' ? [geometry.coordinates] : geometry.coordinates;
  return polygons.some(polygon => {
    const outer = ringPosition(point, polygon[0]);
    if (!outer.inside) return false;
    return !polygon.slice(1).some(hole => { const p = ringPosition(point, hole); return p.inside && !p.boundary; });
  });
}
export function visibilityAt(event, location) {
  const v = event.visibility;
  if (!v || !location) return null;
  const {longitude, latitude, east_m_per_degree: sx, north_m_per_degree: sy} = v.chart;
  const project = point => [(point[0] - longitude) * sx, (point[1] - latitude) * sy];
  const p = project([location.longitude, location.latitude]);
  if (v.calculation_domain ? !regionContains(v.calculation_domain, [location.longitude, location.latitude]) : Math.hypot(...p) > 5000) return {inside:false,distance_m:null,boundary_distance_m:null,minimum_time:null,near_boundary:false,outside_chart:true};
  const inside = regionContains(v.geometry, [location.longitude, location.latitude]);
  let boundaryDistance = Infinity;
  const polygons = v.geometry.type === 'Polygon' ? [v.geometry.coordinates] : v.geometry.coordinates;
  for (const polygon of polygons) for (const ring of polygon) for (let i = 1; i < ring.length; i++) {
    const a = project(ring[i - 1]), b = project(ring[i]), dx = b[0] - a[0], dy = b[1] - a[1];
    const fraction = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy || 1)));
    boundaryDistance = Math.min(boundaryDistance, Math.hypot(p[0] - a[0] - fraction * dx, p[1] - a[1] - fraction * dy));
  }
  const model = v.models.find(m => p[0] >= m.bounds_m[0] - 1e-5 && p[0] <= m.bounds_m[2] + 1e-5 &&
    p[1] >= m.bounds_m[1] - 1e-5 && p[1] <= m.bounds_m[3] + 1e-5);
  let minimumTime = null;
  if (inside && model) {
    const dt = model.time_offset_s + model.time_gradient_s_per_m[0] * (p[0] - model.origin_m[0]) +
      model.time_gradient_s_per_m[1] * (p[1] - model.origin_m[1]);
    minimumTime = new Date(Date.parse(v.reference_time) + dt * 1000).toISOString();
  }
  return {inside, distance_m: inside ? 0 : boundaryDistance, boundary_distance_m: boundaryDistance,
    minimum_time: minimumTime, near_boundary: boundaryDistance <= v.sampled_boundary_tolerance_m};
}
