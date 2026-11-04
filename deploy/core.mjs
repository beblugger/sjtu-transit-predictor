export const PAGE_SIZE = 50;
export const TARGETS = {sun: '太阳', moon: '月球'};
export const WEATHER = {announced: '模型评分达标', suppressed: '模型评分未达标', unavailable: '天气不可用'};
export const REASONS = {
  object_size_unknown_centerline_only: '卫星尺寸未知，计算采用卫星中心穿过日/月视圆盘的条件',
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
export const zoneName = zone => zone === 'UTC' ? 'UTC' : 'UTC+08:00';
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
export function filterEvents(events, filters, location = null) {
  const needle = filters.search.trim().toLowerCase();
  const result = events.filter(event => {
    const t = Date.parse(event.minimum_time), diameter = event.object_angular_diameter_arcsec;
    return t >= filters.from && t < filters.to &&
      (filters.target === 'all' || filters.target === event.target) &&
      (!needle || `${event.name} ${event.identifier}`.toLowerCase().includes(needle)) &&
      event.altitude_deg >= filters.minAltitude &&
      (present(diameter) ? diameter >= filters.minDiameter : filters.keepUnknown) &&
      (filters.weather === 'all' || filters.weather === event.weather.decision);
  });
  result.sort((a, b) => {
    let difference = 0;
    if (filters.sort === 'altitude') difference = b.altitude_deg - a.altitude_deg;
    if (filters.sort === 'diameter') difference = (b.object_angular_diameter_arcsec ?? -1) - (a.object_angular_diameter_arcsec ?? -1);
    if (filters.sort === 'distance' && location) difference = distanceMeters(location, a.observer) - distanceMeters(location, b.observer);
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
    'orbit_epoch_utc', 'orbit_age_at_event_hours', 'weather_decision', 'weather_score', 'reason_codes'];
  const rows = events.map(e => [e.identifier, e.name, e.target, e.start_time, e.minimum_time, e.end_time,
    e.duration_ms / 1000, e.altitude_deg, e.azimuth_deg, e.range_km, e.object_angular_diameter_arcsec, e.physical_size_m,
    e.observer.latitude, e.observer.longitude, e.observer.elevation_m, e.orbit_epoch,
    e.orbit_age_at_event_hours, e.weather.decision, e.weather.score, e.reason_codes.join(';')]);
  return '\ufeff' + [fields, ...rows].map(row => row.map(csvCell).join(',')).join('\r\n') + '\r\n';
}
