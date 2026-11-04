import {PAGE_SIZE, TARGETS, WEATHER, REASONS, present, number, formatTime, zoneName,
  inputTime, inputInstant, eventKey, distanceMeters, filterEvents, timeOpacity, validateSnapshot, eventsCSV} from './core.mjs';

const $ = id => document.getElementById(id);
const state = {manifest: null, events: [], campus: null, filtered: [], page: 0, selected: null,
  location: null, zone: 'Asia/Shanghai', loading: false};
const node = (tag, text, attributes = {}) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
  return element;
};
function dl(id, entries) {
  $(id).replaceChildren(...entries.flatMap(([label, value]) => [node('dt', label), node('dd', value)]));
}
function stamp(value, detailed = false) {
  return value ? `${formatTime(value, state.zone, detailed)} ${zoneName(state.zone)}` : '未知 / 尚未记录';
}
function quantity(value, unit, digits = 2) { return present(value) ? `${number(value, digits)} ${unit}` : '未知'; }
function reason(value) { return REASONS[value] ? `${REASONS[value]}（${value}）` : value; }
function download(name, content, type) {
  const url = URL.createObjectURL(new Blob([content], {type}));
  const a = node('a', undefined, {href: url, download: name});
  document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 2000);
}
async function getBytes(url) {
  const response = await fetch(url, {cache: 'no-store'});
  if (!response.ok) throw new Error(`${url.pathname.split('/').at(-1)}：HTTP ${response.status}`);
  return new Uint8Array(await response.arrayBuffer());
}
async function checkedJSON(filename, manifest) {
  const digest = manifest.files?.[filename];
  if (!/^[a-f0-9]{64}$/.test(digest || '')) throw new Error(`${filename} 缺少有效 SHA256`);
  const url = new URL(`data/${filename}`, location.href); url.searchParams.set('v', digest);
  const bytes = await getBytes(url);
  if (!crypto.subtle) throw new Error('当前环境不能校验数据，请使用 HTTPS 或 localhost 打开');
  const actual = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), b => b.toString(16).padStart(2, '0')).join('');
  if (actual !== digest) throw new Error(`${filename} 校验不一致，可能正在更新`);
  return JSON.parse(new TextDecoder().decode(bytes));
}
async function loadSnapshot() {
  if (state.loading) return;
  state.loading = true; $('reload').disabled = true;
  $('load-status').textContent = '正在读取并校验数据……'; $('load-status').dataset.error = 'false';
  try {
    let snapshot, lastError;
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const url = new URL('data/manifest.json', location.href); url.searchParams.set('read', String(Date.now()));
        const manifest = JSON.parse(new TextDecoder().decode(await getBytes(url)));
        const [payload, campus] = await Promise.all([checkedJSON('events.json', manifest), checkedJSON('campus.geojson', manifest)]);
        validateSnapshot(manifest, payload, campus);
        snapshot = {manifest, payload, campus}; break;
      } catch (error) { lastError = error; }
    }
    if (!snapshot) throw lastError;
    const oldKey = state.selected && eventKey(state.selected);
    Object.assign(state, {manifest: snapshot.manifest, events: snapshot.payload.events, campus: snapshot.campus, page: 0, selected: null});
    $('application').hidden = false; $('filter-controls').disabled = false;
    setWindow(24); renderMetadata(); render();
    const hashKey = new URLSearchParams(location.hash.slice(1)).get('event') || oldKey;
    if (hashKey) {
      const event = state.events.find(e => eventKey(e) === hashKey);
      if (event) { resetFilters(true); render(); selectEvent(event, false); }
      else $('share-status').textContent = '链接所指事件已不在本次数据中，可能已过期或因轨道更新而改变。';
    }
    $('load-status').textContent = `数据加载成功：已按版本清单验证事件与校区文件 SHA256，共 ${state.events.length} 条事件。`;
  } catch (error) {
    $('load-status').dataset.error = 'true';
    $('load-status').textContent = `读取失败：${error.message}。${state.manifest ? '下方仍是上一次成功读取的数据，请核对生成时间。' : '请重试，或下载页尾的原始文件。'}`;
  } finally { state.loading = false; $('reload').disabled = false; }
}
function renderMetadata() {
  const m = state.manifest;
  dl('metadata', [
    ['显示时区', state.zone === 'UTC' ? 'UTC' : '北京时间 UTC+08:00'],
    ['预报生成时间', stamp(m.generated_at)], ['预报覆盖 [起始, 结束)', `${stamp(m.from_time)} → ${stamp(m.to_time)}`],
    ['轨道来源 / 数量', `${m.catalog_source} / ${m.catalog_count}`],
    ['轨道下载时间', stamp(m.catalog_fetched_at)], ['主站最后检查', stamp(m.catalog_checked_at)],
    ['主站已下载版本的发布时间', stamp(m.primary_upstream_updated_at)],
    ['日月星历', m.ephemeris], ['天气模型获取时间', stamp(m.weather_status.model_updated_at)],
    ['轨道目录 SHA256', m.catalog_sha256],
  ]);
  const now = Date.now(), generated = Date.parse(m.generated_at), end = Date.parse(m.to_time);
  $('freshness').textContent = now >= end ? '数据覆盖已经结束，请检查构建状态并重新读取。历史数据仍可查看。' :
    now - generated > 3 * 3600000 ? `本批预报已生成 ${((now - generated) / 3600000).toFixed(1)} 小时，可能未按计划更新。` :
      now < Date.parse(m.from_time) ? '当前设备时间早于预报起点，请检查设备时钟。' : '当前时间位于预报覆盖范围内。';
  const warnings = [...m.warnings];
  if (m.weather_status.stale) warnings.push('天气数据在本批生成时已过期');
  $('warnings').replaceChildren(...warnings.map(w => node('li', w)));
}
function setWindow(hours, all = false) {
  const m = state.manifest;
  const start = all ? Date.parse(m.from_time) : Math.max(Date.now(), Date.parse(m.from_time));
  const end = all ? Date.parse(m.to_time) : Math.min(start + hours * 3600000, Date.parse(m.to_time));
  // Keep expired coverage inspectable; never quietly present it as upcoming.
  $('from').value = inputTime(start, state.zone);
  $('to').value = inputTime(Math.max(start, end), state.zone);
}
function resetFilters(all = false) {
  $('target').value = 'all'; $('search').value = ''; $('min-altitude').value = all ? '0' : '15';
  $('min-diameter').value = '0'; $('keep-unknown').checked = true; $('weather').value = 'all'; $('sort').value = 'time';
  setWindow(24, all); state.page = 0;
}
function readFilters() {
  return {from: inputInstant($('from').value, state.zone), to: inputInstant($('to').value, state.zone),
    target: $('target').value, search: $('search').value, minAltitude: $('min-altitude').valueAsNumber,
    minDiameter: $('min-diameter').valueAsNumber, keepUnknown: $('keep-unknown').checked,
    weather: $('weather').value, sort: $('sort').value};
}
function render() {
  if (!state.manifest) return;
  const f = readFilters();
  const error = !Number.isFinite(f.from) || !Number.isFinite(f.to) ? '请输入完整的起止时间。' :
    f.from >= f.to ? '结束时间必须晚于起始时间；若数据已过期，可选择“全部数据覆盖时段”。' :
    !present(f.minAltitude) || f.minAltitude < 0 || f.minAltitude > 90 || !present(f.minDiameter) || f.minDiameter < 0 ? '高度角须为 0–90°，最小角直径须为非负数。' : '';
  $('filter-error').textContent = error;
  state.filtered = error ? [] : filterEvents(state.events, f, state.location);
  const pages = Math.max(1, Math.ceil(state.filtered.length / PAGE_SIZE));
  state.page = Math.min(state.page, pages - 1);
  if (state.selected && !state.filtered.some(e => eventKey(e) === eventKey(state.selected))) state.selected = null;
  const events = state.filtered.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE);
  $('time-heading').textContent = `最小角距时刻 (${zoneName(state.zone)})`;
  $('result-count').textContent = `${state.filtered.length} / ${state.events.length} 条符合条件。时间区间包含起点、不包含终点。${f.sort === 'distance' && !state.location ? '尚未设置参考位置，暂按时间排序。' : ''}`;
  $('page-status').textContent = `第 ${state.page + 1} / ${pages} 页`;
  $('previous').disabled = state.page === 0; $('next').disabled = state.page + 1 >= pages;
  $('download-csv').disabled = !!error || !state.filtered.length;
  $('events').replaceChildren(...events.map(event => {
    const row = node('tr'); row.setAttribute('aria-selected', String(!!state.selected && eventKey(state.selected) === eventKey(event)));
    const values = [`${event.name} / ${event.identifier}`, TARGETS[event.target], formatTime(event.minimum_time, state.zone),
      number(event.altitude_deg, 1), number(event.duration_ms / 1000, 3), number(event.object_angular_diameter_arcsec, 2),
      number(event.range_km, 1), WEATHER[event.weather.decision], state.location ? number(distanceMeters(state.location, event.observer), 0) : '未设置'];
    values.forEach((value, index) => row.append(node('td', value, [3, 4, 5, 6, 8].includes(index) ? {class: 'number'} : {})));
    const button = node('button', '详情', {type: 'button', 'aria-label': `查看 ${event.name} ${TARGETS[event.target]} ${formatTime(event.minimum_time, state.zone)} 的详情`});
    button.addEventListener('click', () => selectEvent(event, true));
    const cell = node('td'); cell.append(button); row.append(cell); return row;
  }));
  if (!events.length) { const row = node('tr'); row.append(node('td', error || '此时段和筛选条件下没有事件。可扩大时间范围、降低高度限制或恢复默认筛选。', {colspan: 10})); $('events').append(row); }
  drawMap(events); renderDetail();
}
function selectEvent(event, focus) {
  state.selected = event;
  const index = state.filtered.findIndex(e => eventKey(e) === eventKey(event));
  if (index >= 0) state.page = Math.floor(index / PAGE_SIZE);
  const url = new URL(location.href); url.hash = new URLSearchParams({event: eventKey(event)}).toString();
  history.replaceState(null, '', url);
  render();
  if (focus) $('detail').focus();
}
function renderDetail() {
  const e = state.selected;
  $('detail-empty').hidden = !!e; $('detail-content').hidden = !e;
  if (!e) return;
  $('detail-name').textContent = `${e.name} · NORAD ${e.identifier} · 凌${TARGETS[e.target] === '太阳' ? '日' : '月'}`;
  dl('detail-fields', [
    ['观测点 (WGS84)', `纬度 ${number(e.observer.latitude, 5)}°N，经度 ${number(e.observer.longitude, 5)}°E`],
    ['计算使用的观测高度', `${quantity(e.observer.elevation_m, 'm')}（未接入地形 / 建筑高度；不等于现场实测海拔）`],
    ['入凌', stamp(e.start_time, true)], ['最小角距', stamp(e.minimum_time, true)], ['出凌', stamp(e.end_time, true)],
    ['持续时间', quantity(e.duration_ms / 1000, 's', 3)],
    ['卫星高度 / 方位角', `${quantity(e.altitude_deg, '°')} / ${quantity(e.azimuth_deg, '°')}（方位角：北 0°，东 90°）`],
    ['目标高度 / 方位角', `${quantity(e.target_altitude_deg, '°')} / ${quantity(e.target_azimuth_deg, '°')}`],
    ['卫星斜距', quantity(e.range_km, 'km', 2)], ['最小中心角距', quantity(e.min_separation_arcsec, '″', 3)],
    ['日 / 月视半径', quantity(e.target_angular_radius_arcsec, '″', 3)],
    ['卫星角直径（最大尺寸估计）', quantity(e.object_angular_diameter_arcsec, '″', 3)],
    ['卫星角直径范围', `${quantity(e.object_angular_diameter_min_arcsec, '″', 3)} — ${quantity(e.object_angular_diameter_max_arcsec, '″', 3)}`],
    ['卫星物理尺寸范围', `${quantity(e.physical_size_min_m, 'm')} — ${quantity(e.physical_size_max_m, 'm')}`],
    ['尺寸可信度', e.physical_size_confidence || '未知'], ['轨道历元', stamp(e.orbit_epoch, true)],
    ['距轨道历元的外推时长', quantity(e.orbit_age_at_event_hours, 'h', 2)],
    ['距参考点', state.location ? `${quantity(distanceMeters(state.location, e.observer), 'm', 0)}（到优选点的直线距离，不是到可见区的距离或步行距离）` : '未设置参考位置'],
  ]);
  if (e.physical_size_source) {
    const url = new URL(e.physical_size_source, location.href);
    if (['https:', 'http:'].includes(url.protocol)) {
      $('detail-fields').append(node('dt', '尺寸来源'));
      const dd = node('dd'); dd.append(node('a', url.href, {href: url.href, target: '_blank', rel: 'noopener noreferrer'})); $('detail-fields').append(dd);
    }
  }
  const w = e.weather;
  const score = value => w.decision === 'unavailable' ? '不可用' : number(value, 3);
  dl('weather-fields', [
    ['天气判断 / 评分', `${WEATHER[w.decision]} / ${score(w.score)}（0–1 模型指标）`],
    ['P0 / P3 / P12', `${score(w.p0_clear)} / ${score(w.p3_clear)} / ${score(w.p12_clear)}（事件时刻 / ±3 h / ±12 h）`],
    ['各模型评分', Object.entries(w.model_scores).map(([name, value]) => `${name}: ${number(value, 3)}`).join('；') || '无'],
    ['模型分歧 / 运行间变化', `${score(w.model_gap)} / ${score(w.run_change)}`],
    ['判定阈值', score(w.threshold)], ['确定性天气获取时间', stamp(w.observation_time)], ['集合预报获取时间', stamp(w.model_updated_at)],
  ]);
  const reasons = [...new Set([...e.reason_codes, ...w.reasons])];
  $('detail-reasons').replaceChildren(...reasons.map(r => node('li', reason(r))), node('li', '尚无经过量化的轨道位置 / 凌星时刻误差范围；不据此给出百米可见保证。'));
  $('raw-event').textContent = JSON.stringify(e, null, 2);
}
const SVG = 'http://www.w3.org/2000/svg';
function svg(tag, attrs = {}, text) {
  const element = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
  if (text !== undefined) element.textContent = text;
  return element;
}
function drawMap(events) {
  const map = $('map');
  const polygons = state.campus.features.flatMap(f => f.geometry.type === 'MultiPolygon' ? f.geometry.coordinates : [f.geometry.coordinates]);
  const points = polygons.flat(2);
  const lon0 = (Math.min(...points.map(p => p[0])) + Math.max(...points.map(p => p[0]))) / 2;
  const lat0 = (Math.min(...points.map(p => p[1])) + Math.max(...points.map(p => p[1]))) / 2;
  const meters = ([lon, lat]) => [(lon - lon0) * 111320 * Math.cos(lat0 * Math.PI / 180), (lat - lat0) * 111320];
  const xy = points.map(meters), minX = Math.min(...xy.map(p => p[0])), maxX = Math.max(...xy.map(p => p[0]));
  const minY = Math.min(...xy.map(p => p[1])), maxY = Math.max(...xy.map(p => p[1]));
  const scale = Math.min(820 / (maxX - minX), 420 / (maxY - minY));
  const project = p => { const [x, y] = meters(p); return [480 + x * scale, 280 - y * scale]; };
  map.replaceChildren(svg('title', {id: 'map-description'}, 'WGS84 校区范围和当前列表页的优选观测点'));
  for (const polygon of polygons) {
    const path = polygon.map(ring => ring.map((p, i) => `${i ? 'L' : 'M'}${project(p).join(',')}`).join(' ') + 'Z').join(' ');
    map.append(svg('path', {d: path, fill: '#f2f2f2', stroke: '#555', 'stroke-width': 1.5, 'fill-rule': 'evenodd'}));
  }
  map.append(svg('path', {d: 'M36 78V24M30 34L36 24L42 34', stroke: '#161616', fill: 'none', 'stroke-width': 2}), svg('text', {x: 29, y: 100}, '北'));
  const bar = 500 * scale;
  map.append(svg('path', {d: `M60 510v8h${bar}v-8`, fill: 'none', stroke: '#161616', 'stroke-width': 2}), svg('text', {x: 60, y: 542}, '500 m（近似）'));
  for (const event of events) {
    const [x, y] = project([event.observer.longitude, event.observer.latitude]);
    const color = event.target === 'sun' ? '#9c4100' : '#0072b2';
    const selected = state.selected && eventKey(state.selected) === eventKey(event);
    const marker = event.target === 'sun' ? svg('circle', {cx: x, cy: y, r: selected ? 8 : 5}) :
      svg('path', {d: `M${x} ${y - (selected ? 10 : 7)}l${selected ? 10 : 7} ${selected ? 10 : 7}l-${selected ? 10 : 7} ${selected ? 10 : 7}l-${selected ? 10 : 7} -${selected ? 10 : 7}Z`});
    for (const [key, value] of Object.entries({fill: color, 'fill-opacity': timeOpacity(event.minimum_time), stroke: selected ? '#161616' : color,
      'stroke-width': selected ? 3 : 1.5, class: 'marker', role: 'button', tabindex: 0,
      'aria-label': `${event.name}，${TARGETS[event.target]}，${formatTime(event.minimum_time, state.zone)} ${zoneName(state.zone)}`})) marker.setAttribute(key, value);
    marker.append(svg('title', {}, `${event.name} / ${event.identifier} / ${stamp(event.minimum_time)}`));
    marker.addEventListener('click', () => selectEvent(event, true));
    marker.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectEvent(event, true); } });
    map.append(marker);
  }
  if (state.selected && events.includes(state.selected)) {
    const event = state.selected, [x, y] = project([event.observer.longitude, event.observer.latitude]);
    const text = svg('text', {x: Math.max(80, Math.min(x + 12, 700)), y: Math.max(25, y - 15), 'paint-order': 'stroke', stroke: '#fff', 'stroke-width': 4}, `${event.name} ${formatTime(event.minimum_time, state.zone).slice(11)} ${zoneName(state.zone)}`);
    map.append(text);
  }
  let locationText = '';
  if (state.location) {
    const [x, y] = project([state.location.longitude, state.location.latitude]);
    if (x >= 0 && x <= 960 && y >= 0 && y <= 560) {
      map.append(svg('path', {d: `M${x - 10} ${y}h20M${x} ${y - 10}v20`, stroke: '#161616', 'stroke-width': 3}));
    } else locationText = '参考位置在图外。';
  }
  $('map-status').textContent = `本页 ${events.length} 个优选点，点可能重叠；可从列表逐一选择。${locationText}`;
}
$('reload').addEventListener('click', loadSnapshot);
$('filters').addEventListener('submit', event => event.preventDefault());
$('filters').addEventListener('input', event => { if (event.target.id !== 'timezone') { state.page = 0; render(); } });
$('timezone').addEventListener('change', () => {
  const from = inputInstant($('from').value, state.zone), to = inputInstant($('to').value, state.zone);
  state.zone = $('timezone').value;
  if (Number.isFinite(from)) $('from').value = inputTime(from, state.zone);
  if (Number.isFinite(to)) $('to').value = inputTime(to, state.zone);
  renderMetadata(); render();
});
for (const button of document.querySelectorAll('[data-hours]')) button.addEventListener('click', () => { setWindow(Number(button.dataset.hours)); state.page = 0; render(); });
$('all-coverage').addEventListener('click', () => { setWindow(96, true); state.page = 0; render(); });
$('reset').addEventListener('click', () => { resetFilters(); render(); });
$('previous').addEventListener('click', () => { state.page--; render(); });
$('next').addEventListener('click', () => { state.page++; render(); });
$('location-form').addEventListener('submit', event => {
  event.preventDefault(); state.location = {latitude: $('latitude').valueAsNumber, longitude: $('longitude').valueAsNumber};
  $('location-status').textContent = '已设置 WGS84 参考位置；列表距离为到优选点的地表高度零近似直线距离，不代表可见范围。'; render();
});
$('clear-location').addEventListener('click', () => { state.location = null; $('location-status').textContent = '已清除参考位置。'; render(); });
$('locate').addEventListener('click', () => {
  if (!navigator.geolocation) { $('location-status').textContent = '此浏览器不支持定位，请手动输入 WGS84 坐标。'; return; }
  $('location-status').textContent = '正在请求设备位置……';
  navigator.geolocation.getCurrentPosition(position => {
    state.location = {latitude: position.coords.latitude, longitude: position.coords.longitude};
    $('latitude').value = state.location.latitude; $('longitude').value = state.location.longitude;
    $('location-status').textContent = `设备定位精度报告：约 ${number(position.coords.accuracy, 0)} m；此精度不是凌星预测误差。位置仅在本机使用。`; render();
  }, error => { $('location-status').textContent = `定位失败：${error.message}。可手动输入 WGS84 坐标。`; }, {enableHighAccuracy: true, timeout: 15000, maximumAge: 30000});
});
$('download-csv').addEventListener('click', () => download('transits-utc.csv', eventsCSV(state.filtered), 'text/csv;charset=utf-8'));
$('download-event').addEventListener('click', () => { if (state.selected) download(`transit-${state.selected.identifier}.json`, JSON.stringify(state.selected, null, 2), 'application/json'); });
$('share').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(location.href); $('share-status').textContent = '事件链接已复制；数据更新后事件可能改变。'; }
  catch { $('share-status').textContent = `请复制地址栏链接：${location.href}`; }
});
setInterval(() => { if (state.manifest) { renderMetadata(); render(); } }, 60000);
loadSnapshot();
