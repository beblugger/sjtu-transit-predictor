import {weatherCondition,visibleDetailEntries,dataNotices,brightnessLabel} from './presentation.mjs';
import {UPDATE_INTERVAL,REMINDER_INTERVAL,SETTINGS_KEY,SENT_KEY,readSettings,nearbyEvents,reminderKey,alreadyReminded,notificationURL} from './reminders.mjs';
import {mapView,exteriorTiles,campusTiles,overlayEvents,nearestEvent,SELECTED_COLOR,NEIGHBOR_COLOR,asLines} from './map.mjs';
import {PAGE_SIZE, TARGETS, WEATHER, REASONS, present, number, formatTime, zoneName,
  inputTime, inputInstant, eventKey, filterEvents, timeOpacity, validateSnapshot, visibilityAt} from './core.mjs';

const $ = id => document.getElementById(id);
const state = {manifest: null, events: [], campus: null, filtered: [], page: 0, selected: null, sharedKey:null,
  weather: null,buildings:null,backend:null,loadError:'',assetWarnings:[],location: null, zone: 'Asia/Shanghai', loading: false};
let settings;
try {settings=readSettings(localStorage);}catch{settings=readSettings(null);}
let lastRead=0;
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
function reason(value) { return REASONS[value] || value; }
async function getBytes(url) {
  const response = await fetch(url, {cache: 'no-store',signal:AbortSignal.timeout(20000)});
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
  const initial=!state.manifest;
  lastRead=Date.now();
  state.loadError='';state.assetWarnings=state.assetWarnings.filter(w=>!w.startsWith('天气文件'));
  state.loading = true; $('reload').disabled = true;
  $('load-status').textContent = '正在读取并校验数据……'; $('load-status').dataset.error = 'false';
  try {
    let snapshot, lastError, offline=false;
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const url = new URL('data/manifest.json', location.href); url.searchParams.set('read', String(Date.now()));
        const manifest = JSON.parse(new TextDecoder().decode(await getBytes(url)));
        const [payload, campus] = await Promise.all([checkedJSON('events.json', manifest), checkedJSON('campus.geojson', manifest)]);
        validateSnapshot(manifest, payload, campus);
        snapshot = {manifest, payload, campus}; break;
      } catch (error) { lastError = error; }
    }
    if (!snapshot) {
      if(!initial)throw lastError;
      try {
        const cached=await (await caches.open('transit-snapshot-v1')).match(new URL('data/verified-snapshot',location.href));
        snapshot=await cached.json();
        validateSnapshot(snapshot.manifest,snapshot.payload,snapshot.campus);
        offline=true;
      } catch {throw lastError;}
    }
    let weather=null;
    if(offline)weather=snapshot.weather;
    else try{weather=await checkedJSON('weather.json',snapshot.manifest);}catch{state.assetWarnings.push('天气文件读取或校验失败，阴晴预报暂不可用。');}
    if(!offline) {
      try {
        const cache=await caches.open('transit-snapshot-v1');
        await cache.put(new URL('data/verified-snapshot',location.href),new Response(JSON.stringify({...snapshot,weather}),{headers:{'Content-Type':'application/json'}}));
      } catch {/* Online data remain usable if storage is unavailable. */}
    }
    state.weather=weather;
    const oldKey = state.selected && eventKey(state.selected);
    Object.assign(state, {manifest: snapshot.manifest, events: snapshot.payload.events, campus: snapshot.campus});
    state.selected=state.events.find(e=>eventKey(e)===oldKey)||null;
    $('application').hidden = false; $('filter-controls').disabled = false;
    // Default to the full snapshot coverage so past and future events are all inspectable.
    if(initial)setWindow(0, true);
    renderMetadata(); render();
    const hashKey = initial && new URLSearchParams(location.hash.slice(1)).get('event');
    if (hashKey) {
      const event = state.events.find(e => eventKey(e) === hashKey);
      if (event) { resetFilters(true, true);selectEvent(event,false,true); }
      else $('share-status').textContent = '链接所指事件已不在本次数据中，可能已过期或因轨道更新而改变。';
    }
    if(offline)state.loadError='无法连接网络，正在显示本机保存的数据，时效以数据生成时间为准。';
    $('load-status').textContent = offline?state.loadError:`已加载 ${state.events.length} 条事件，数据校验通过；检查于 ${formatTime(Date.now(),state.zone)}。`;
    $('load-status').dataset.error=String(offline);
  } catch (error) {
    $('load-status').dataset.error = 'true';
    state.loadError=`读取失败：${error.message}。${state.manifest ? '仍显示上一次读取的数据。' : '请重试。'}`;
    $('load-status').textContent=state.loadError;
  } finally { state.loading = false; $('reload').disabled = false; renderNotices();checkBackend();checkNearby(); }
}
function renderMetadata() {
  const m = state.manifest;
  dl('metadata', [
    ['显示时区', 'CST（中国标准时间，UTC+08:00）'],
    ['预报生成时间', stamp(m.generated_at)], ['预报覆盖 [起始, 结束)', `${stamp(m.from_time)} → ${stamp(m.to_time)}`],
    ['轨道来源 / 数量', `${m.catalog_source} / ${m.catalog_count}`],
    ['轨道下载时间', stamp(m.catalog_fetched_at)], ['主站最后检查', stamp(m.catalog_checked_at)],
    ['主站已下载版本的发布时间', stamp(m.primary_upstream_updated_at)],
    ['日月星历', m.ephemeris], ['天气模型获取时间', stamp(m.weather_status.model_updated_at)],
    ['轨道目录 SHA256', m.catalog_sha256],
  ]);
  renderNotices();
}
function setWindow(hours, all = false) {
  const m = state.manifest;
  const start = all ? Date.parse(m.from_time) : Math.max(Date.now(), Date.parse(m.from_time));
  const end = all ? Date.parse(m.to_time) : Math.min(start + hours * 3600000, Date.parse(m.to_time));
  // Keep expired coverage inspectable; never quietly present it as upcoming.
  $('from').value = inputTime(start, state.zone);
  $('to').value = inputTime(Math.max(start, end), state.zone);
}
function resetFilters(fullCoverage = true, relaxed = false) {
  state.sharedKey=null;
  $('target').value = 'all'; $('search').value = ''; $('min-altitude').value = relaxed ? '0' : '15';
  $('inside-only').checked = false; $('weather').value = 'clear'; $('max-distance').value = ''; $('sort').value = 'time';
  setWindow(0, fullCoverage); state.page = 0;
}
function readFilters() {
  return {from: inputInstant($('from').value, state.zone), to: inputInstant($('to').value, state.zone),
    target: $('target').value, search: $('search').value, minAltitude: $('min-altitude').valueAsNumber,
    minDiameter: 0, keepUnknown: true,
    maxDistance: state.location && $('max-distance').value !== '' ? $('max-distance').valueAsNumber : null,
    weather: $('weather').value, sort: $('sort').value, insideOnly: $('inside-only').checked};
}
function render() {
  if (!state.manifest) return;
  for(const field of ['weather','target'])for(const tab of document.querySelectorAll(`[data-${field}]`)){const active=tab.dataset[field]===$(field).value;tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;}
  const f = readFilters();
  const error = !Number.isFinite(f.from) || !Number.isFinite(f.to) ? '请输入完整的起止时间。' :
    f.from >= f.to ? '结束时间必须晚于起始时间；若数据已过期，可选择“全部数据覆盖时段”。' :
    f.maxDistance !== null && (!Number.isFinite(f.maxDistance) || f.maxDistance < 0) ? '距可见区最大距离须为非负数，留空表示不限。' :
    !present(f.minAltitude) || f.minAltitude < 0 || f.minAltitude > 90 || !present(f.minDiameter) || f.minDiameter < 0 ? '高度角须为 0–90°，最小角直径须为非负数。' : '';
  $('filter-error').textContent = error;
  state.filtered = error ? [] : filterEvents(state.events, f, state.location,state.weather);
  const pages = Math.max(1, Math.ceil(state.filtered.length / PAGE_SIZE));
  state.page = Math.min(state.page, pages - 1);
  if (state.selected && state.sharedKey!==eventKey(state.selected) && !state.filtered.some(e => eventKey(e) === eventKey(state.selected))) state.selected = null;
  if (!state.selected) {
    state.selected = nearestEvent(state.filtered);
    if (state.selected) state.page = Math.floor(state.filtered.indexOf(state.selected) / PAGE_SIZE);
  }
  const overlays = currentOverlays();
  $('result-count').textContent = `${state.filtered.length} / ${state.events.length} 条符合条件。${f.sort === 'distance' && !state.location ? '尚未设置位置，暂按时间排序。' : ''}`;
  $('selected-event').textContent=state.selected ? `已选：${state.selected.name} / ${state.selected.identifier} · ${formatTime(state.selected.minimum_time,state.zone)}${state.sharedKey&&!state.filtered.includes(state.selected)?'（链接事件不符合当前筛选）':''}` : '尚无选中事件';
  const rows=state.filtered.map(event => {
    const row = node('tr'); row.dataset.eventKey = eventKey(event);
    row.dataset.overlay = String(overlays.includes(event)); row.setAttribute('aria-selected', String(!!state.selected && eventKey(state.selected) === eventKey(event)));
    const values = [`${event.name} / ${event.identifier}`, TARGETS[event.target], formatTime(event.minimum_time, state.zone),
      number(event.altitude_deg, 1), number(event.duration_ms / 1000, 3), number(event.object_angular_diameter_arcsec, 2),
      number(event.range_km, 1), weatherCondition(state.weather,event.minimum_time).label, state.location ? number(visibilityAt(event, state.location)?.distance_m, 0) : '未设置'];
    values.forEach((value, index) => row.append(node('td', value, [3, 4, 5, 6, 8].includes(index) ? {class: 'number'} : {})));
    row.tabIndex = 0;
    row.setAttribute('aria-label', `查看 ${event.name} ${TARGETS[event.target]} ${formatTime(event.minimum_time, state.zone)} 的详情`);
    const choose = () => { selectEvent(event, false); $('events').querySelector('tr[aria-selected=true]')?.focus({preventScroll:true}); };
    row.addEventListener('click', choose);
    row.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); choose(); }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault(); const next = e.key === 'ArrowDown' ? row.nextElementSibling : row.previousElementSibling;
        if (next?.hasAttribute('data-event-key')) next.focus();
      }
    });
    return row;
  });
  renderEventPages(rows,pages,error);
  $('weather-tooltip').hidden=true;
  drawMap(); renderDetail();renderNotices();
}
function renderEventPages(rows,pages,error){
  const headers=['卫星 / NORAD','目标',`最近时刻 (${zoneName(state.zone)})`,'高度 (°)','持续 (s)','角直径 (″)','距离 (km)','天气','距可见区 (m)'];
  const groups=[];
  for(let i=0;i<pages;i++){
    const group=node('div',undefined,{class:'event-page','data-page':i,role:'group','aria-label':`第 ${i+1} 页`});
    const table=node('table'),head=node('thead'),heading=node('tr');
    headers.forEach((label,column)=>{
      const th=node('th',label,{scope:'col'});
      if([3,4,5,6,8].includes(column))th.className='number';
      if(column===7){th.classList.add('weather-heading');th.dataset.weatherTrigger='';th.setAttribute('aria-describedby','weather-tooltip');}
      heading.append(th);
    });
    head.append(heading);const body=node('tbody');
    const pageRows=rows.slice(i*PAGE_SIZE,(i+1)*PAGE_SIZE);body.append(...pageRows);
    if(!rows.length){const row=node('tr');row.append(node('td',error||'此时段和筛选条件下没有事件。可扩大时间范围、降低高度限制或恢复默认筛选。',{colspan:9}));body.append(row);}
    else for(let empty=pageRows.length;empty<PAGE_SIZE;empty++){
      const row=node('tr',undefined,{'aria-hidden':'true',class:'empty-event-row'});for(let j=0;j<headers.length;j++)row.append(node('td',''));body.append(row);
    }
    if(i===0){const headerTable=node('table',undefined,{class:'events-header'});headerTable.append(head);$('events-header').replaceChildren(headerTable);}
    table.append(body);
    for(const target of [table,...(i===0?[$('events-header').firstChild]:[])]){
      const columns=node('colgroup');for(const width of [19,6,17,8,8,10,10,9,13])columns.append(node('col',undefined,{style:`width:${width}%`}));target.prepend(columns);
    }
    group.append(table);groups.push(group);
  }
  const scroller=$('events');scroller.replaceChildren(...groups);scroller.scrollTop=state.page*scroller.clientHeight;updatePageControls();
}
function updatePageControls(){
  const pages=Math.max(1,Math.ceil(state.filtered.length/PAGE_SIZE));
  $('page-status').textContent=`第 ${state.page+1} / ${pages} 页`;
  $('previous').disabled=state.page===0;$('next').disabled=state.page+1>=pages;
  $('page-jump').max=String(pages);$('page-jump').value=String(state.page+1);
  for(const group of $('events').children){const active=Number(group.dataset.page)===state.page;group.inert=!active;group.setAttribute('aria-hidden',String(!active));}
}
function goToPage(page){
  const pages=Math.max(1,Math.ceil(state.filtered.length/PAGE_SIZE)),next=Math.max(0,Math.min(pages-1,page));
  if(!Number.isInteger(next))return;
  $('events').scrollTo({top:next*$('events').clientHeight,behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});
}
$('events').addEventListener('scroll',()=>{
  const pages=Math.max(1,Math.ceil(state.filtered.length/PAGE_SIZE)),page=Math.max(0,Math.min(pages-1,Math.round($('events').scrollTop/$('events').clientHeight)));
  if(page!==state.page){state.page=page;updatePageControls();}
},{passive:true});
$('events').addEventListener('keydown',e=>{
  if(e.target!==$('events'))return;
  if(['PageDown','PageUp','Home','End'].includes(e.key)){e.preventDefault();goToPage(e.key==='Home'?0:e.key==='End'?Math.ceil(state.filtered.length/PAGE_SIZE)-1:state.page+(e.key==='PageDown'?1:-1));}
});
if('ResizeObserver' in window)new ResizeObserver(()=>{if(state.manifest)$('events').scrollTop=state.page*$('events').clientHeight;}).observe($('events'));
function selectEvent(event, focus, shared=false) {
  state.sharedKey=shared?eventKey(event):null;
  state.selected = event;
  const index = state.filtered.findIndex(e => eventKey(e) === eventKey(event));
  if (index >= 0) state.page = Math.floor(index / PAGE_SIZE);
  const url = new URL(location.href); url.hash = new URLSearchParams({event: eventKey(event)}).toString();
  history.replaceState(null, '', url);
  render();
  if (focus) $('detail').focus();
}
function selectSharedEvent() {
  if (!state.manifest || state.loading) return;
  const key = new URLSearchParams(location.hash.slice(1)).get('event');
  if (!key) return;
  const event = state.events.find(e => eventKey(e) === key);
  if (event) { resetFilters(true, true);selectEvent(event,false,true); }
  else $('share-status').textContent = '链接所指事件已不在本次数据中，可能已过期或因轨道更新而改变。';
}
function renderDetail() {
  const e = state.selected;
  $('detail-empty').hidden = !!e; $('detail-content').hidden = !e;
  if (!e) return;
  drawTransit(e);
  const local = visibilityAt(e, state.location);
  const radius=present(e.object_angular_radius_arcsec)?e.object_angular_radius_arcsec:present(e.object_angular_diameter_arcsec)?e.object_angular_diameter_arcsec/2:null;
  const sizeMethod=e.angular_diameter_method==='rcs_sphere'?'RCS 等效估算':'最大跨度估算';
  $('detail-name').textContent = `${e.name} · NORAD ${e.identifier} · 凌${TARGETS[e.target] === '太阳' ? '日' : '月'}`;
  const entries=visibleDetailEntries([
    ['计算使用的 GNSS 椭球高', `${quantity(e.observer.elevation_m, 'm')}（未接入地形 / 建筑高度）`],
    ['最近时刻',stamp(e.minimum_time,true)],
    ['持续时间', quantity(e.duration_ms / 1000, 's', 3)],
    ['亮度',brightnessLabel(e)],
    ['亮度模型',({'stellarium-starlink':'Stellarium · Starlink 经验模型','stellarium-standard':'Stellarium · 标准星等与相位修正','stellarium-rcs':'Stellarium · 雷达截面积近似'})[e.satellite_magnitude_model]],
    ['卫星相位角',quantity(e.satellite_phase_angle_deg,'°',2)],
    ['天气',weatherCondition(state.weather,e.minimum_time).label],
    ['云量',quantity(weatherCondition(state.weather,e.minimum_time).cloud,'%',0)],
    ['月面照明',e.target==='moon'&&present(e.transit_diagram?.illuminated_fraction)?quantity(e.transit_diagram.illuminated_fraction*100,'%',1):null],
    ['卫星高度 / 方位角', `${quantity(e.altitude_deg, '°')} / ${quantity(e.azimuth_deg, '°')}（方位角：北 0°，东 90°）`],
    ['目标高度 / 方位角', `${quantity(e.target_altitude_deg, '°')} / ${quantity(e.target_azimuth_deg, '°')}`],
    ['卫星距离', quantity(e.range_km, 'km', 2)], ['最小中心角距', quantity(e.min_separation_arcsec, '″', 3)],
    ['日 / 月视半径', quantity(e.target_angular_radius_arcsec, '″', 3)],
    ['卫星角半径 / 角直径', `${quantity(radius,'″',3)} / ${quantity(e.object_angular_diameter_arcsec,'″',3)}（${sizeMethod}）`],
    ['卫星角直径范围', `${quantity(e.object_angular_diameter_min_arcsec, '″', 3)} — ${quantity(e.object_angular_diameter_max_arcsec, '″', 3)}`],
    ['卫星物理尺寸范围', `${quantity(e.physical_size_min_m, 'm')} — ${quantity(e.physical_size_max_m, 'm')}`],
    ['等效直径（RCS 估算）',quantity(e.equivalent_diameter_m,'m',2)],
    ['雷达截面积（RCS）',quantity(e.satellite_rcs_m2,'m²',3)],
    ['尺寸可信度', ({high:'高',medium:'中',low:'低'})[e.angular_size_confidence||e.physical_size_confidence] || '未知'], ['轨道历元', stamp(e.orbit_epoch, true)],
    ['距轨道历元的外推时长', quantity(e.orbit_age_at_event_hours, 'h', 2)],
    ['距可见区', state.location ? quantity(visibilityAt(e, state.location)?.distance_m, 'm', 0) : '未设置位置'],
    ['位置的区域判断', !state.location ? '未设置位置' : !local || local.outside_chart ? '超出计算范围或缺少区域数据，无法判断' : local.inside ? '在反解区域内' : '不在反解区域内'],
    ['位置的边界提示', visibilityAt(e, state.location)?.outside_chart ? '位置不在本次计算范围内' : visibilityAt(e, state.location)?.near_boundary ? '距边界不足 5 m，区域判断对展开误差敏感，应完整复算' : '轨道真实误差尚未量化'],
    ['位置的最近时刻（约）', stamp(visibilityAt(e, state.location)?.minimum_time)],
    ['校内可见区域面积', quantity((e.visibility?.campus_area_m2 ?? e.visibility?.area_m2) / 1e6, 'km²', 3)],
    ['接触边界最大采样回算偏差', e.visibility?.contact_validation_count ? quantity(e.visibility.maximum_sampled_boundary_error_m, 'm', 3) : '本区域未采到日月接触边界'],
    ['局部时刻最大采样回算偏差', quantity(e.visibility?.maximum_sampled_time_error_s, 's', 3)],
    ['反解分片 / 接触验证点', `${e.visibility?.models.length ?? 0} / ${e.visibility?.contact_validation_count ?? 0}`],
  ]);
  const technical=new Set(['亮度模型','卫星相位角','雷达截面积（RCS）','计算使用的 GNSS 椭球高','目标高度 / 方位角','最小中心角距','日 / 月视半径','轨道历元','距轨道历元的外推时长','位置的边界提示','校内可见区域面积','接触边界最大采样回算偏差','局部时刻最大采样回算偏差','反解分片 / 接触验证点']);
  dl('detail-fields',entries.filter(([name])=>!technical.has(name)));
  dl('calculation-fields',entries.filter(([name])=>technical.has(name)));
  if (e.angular_size_source||e.physical_size_source) {
    const url = new URL(e.angular_size_source||e.physical_size_source, location.href);
    if (['https:', 'http:'].includes(url.protocol)) {
      $('detail-fields').append(node('dt', '尺寸来源'));
      const dd = node('dd'); dd.append(node('a', e.angular_diameter_method==='rcs_sphere'?'Stellarium 卫星参数':url.hostname, {href: url.href, target: '_blank', rel: 'noopener noreferrer'})); $('detail-fields').append(dd);
    }
  }

}
let mapZoom=2.5,mapFocus=null,currentView=null;
const SVG = 'http://www.w3.org/2000/svg';
function svg(tag, attrs = {}, text) {
  const element = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
  if (text !== undefined) element.textContent = text;
  return element;
}
function currentOverlays(){
  const events=state.selected&&!state.filtered.includes(state.selected)?[...state.filtered,state.selected]:state.filtered;
  return overlayEvents(events,state.selected);
}
function drawMap() {
  const map=$('map'),overlays=currentOverlays();
  const view=mapView(state.campus,state.selected?.visibility,mapZoom,mapFocus),project=view.project;
  currentView=view;
  $('zoom-level').textContent=`${number(mapZoom,1)}×`;
  $('zoom-out').disabled=mapZoom<=1;$('zoom-in').disabled=mapZoom>=16;
  const polygons=state.campus.features.flatMap(f=>f.geometry.type==='MultiPolygon'?f.geometry.coordinates:[f.geometry.coordinates]);
  map.replaceChildren(svg('title',{id:'map-description'},'卫星凌星区域叠加预测图'));
  const pathFor=polygon=>polygon.map(ring=>ring.map((p,i)=>`${i?'L':'M'}${project(p).join(',')}`).join(' ')+'Z').join(' ');
  const domain=state.selected?.visibility?.calculation_domain;
  const defs=svg('defs'),clip=svg('clipPath',{id:'prediction-clip'});
  if(domain)clip.append(svg('path',{d:pathFor(domain.coordinates)}));
  else clip.append(svg('rect',{width:960,height:560}));
  defs.append(clip);map.append(defs);
  const layers=svg('g',{'clip-path':'url(#prediction-clip)',id:'map-geography','pointer-events':'none'});map.append(layers);
  {
    const exterior=svg('clipPath',{id:'exterior-clip'}),inside=svg('clipPath',{id:'campus-clip'});
    // Use the actual campus polygons, not the raster's rectangular bounds.
    const campusPath=polygons.map(pathFor).join(' ');
    inside.append(svg('path',{d:campusPath,'clip-rule':'evenodd'}));
    exterior.append(svg('path',{d:`M0 0H960V560H0Z ${campusPath}`,'clip-rule':'evenodd'}));
    defs.append(exterior,inside);
    const outside=svg('g',{'clip-path':'url(#exterior-clip)'});layers.append(outside);
    for(const tile of exteriorTiles(view,map.clientWidth)){
      const image=svg('image',{href:tile.url,x:tile.x,y:tile.y,width:tile.size,height:tile.size,crossorigin:'anonymous',class:'exterior-tile'});
      image.addEventListener('error',()=>mapAssetError('部分校外底图暂不可用。'),{once:true});outside.append(image);
    }
    const campusLayers=svg('g',{'clip-path':'url(#campus-clip)'});layers.append(campusLayers);
    // Same WMS layer and tiled EPSG:3857 grid as the source frontend.
    // Select tiles at the browser's current display scale, never shrink a campus-wide screenshot.
    for(const tile of campusTiles(view,map.clientWidth,window.devicePixelRatio)){
      const image=svg('image',{href:tile.url,x:tile.x,y:tile.y,width:tile.size,height:tile.size,class:'campus-map-tile'});
      image.addEventListener('error',()=>{image.remove();mapAssetError('部分校内地图暂不可用，仍显示建筑边界。');},{once:true});campusLayers.append(image);
    }
    // True vector boundaries remain crisp above the source's raster cartography.
    if(state.buildings)for(const feature of state.buildings.features){
      const buildingPolygons=feature.geometry.type==='MultiPolygon'?feature.geometry.coordinates:[feature.geometry.coordinates];
      for(const polygon of buildingPolygons)campusLayers.append(svg('path',{d:pathFor(polygon),fill:'none',stroke:'#777','stroke-opacity':.35,'stroke-width':.5,'fill-rule':'evenodd',class:'campus-building'}));
    }
  }
  for(const polygon of polygons)layers.append(svg('path',{d:pathFor(polygon),fill:'none',stroke:'#666','stroke-width':1}));
  const drawOrder=overlays.map((event,index)=>({event,index})).sort((a,b)=>Number(a.event===state.selected)-Number(b.event===state.selected));
  for(const {event,index} of drawOrder){
    const v=event.visibility,selected=event===state.selected,color=selected?SELECTED_COLOR:NEIGHBOR_COLOR;
    const areas=v.geometry.type==='MultiPolygon'?v.geometry.coordinates:[v.geometry.coordinates];
    for(const polygon of areas){
      const path=svg('path',{d:pathFor(polygon),fill:color,'fill-opacity':selected ? .28 : .07*timeOpacity(event.minimum_time),
        stroke:'none','fill-rule':'evenodd',class:'visibility-region','data-selected':String(selected),'data-event-key':eventKey(event),'data-satellite':event.identifier});
      layers.append(path);
    }
    // Only true angular-contact boundaries; never outline the computation box.
    for(const line of asLines(v.boundaries))layers.append(svg('path',{d:line.map((p,i)=>`${i?'L':'M'}${project(p).join(',')}`).join(' '),
      fill:'none',stroke:color,'stroke-width':selected?2.7:1.8,class:'visibility-boundary','data-satellite':event.identifier}));
    const centerlines=asLines(v.centerline);
    for(const line of centerlines){
      layers.append(svg('path',{d:line.map((p,i)=>`${i?'L':'M'}${project(p).join(',')}`).join(' '),fill:'none',stroke:color,
        'stroke-width':selected?2.5:1.8,'stroke-dasharray':'8 5',class:'visibility-centerline','data-satellite':event.identifier}));
    }

  }
  if(state.location){const [x,y]=project([state.location.longitude,state.location.latitude]);
    if(x>=0&&x<=960&&y>=0&&y<=560)layers.append(svg('path',{d:`M${x-8} ${y}h16M${x} ${y-8}v16`,stroke:'#161616','stroke-width':2}));}
  map.append(svg('path',{d:'M28 60V20M23 29L28 20L33 29',stroke:'#161616',fill:'none','stroke-width':2}),svg('text',{x:21,y:80},'北'));
  const center=state.campus.features[0].properties.center;
  if(center){const a=project(center),b=project([center[0]+500/(111320*Math.cos(center[1]*Math.PI/180)),center[1]]),pixelsPerMeter=Math.abs(b[0]-a[0])/500;
    const meters=[1,2,5,10,20,50,100,200,500,1000].filter(m=>m*pixelsPerMeter<=140).at(-1)??1,length=meters*pixelsPerMeter;
    map.append(svg('path',{d:`M24 530v6h${length}v-6`,stroke:'#161616',fill:'none','stroke-width':2}),svg('text',{x:24,y:519,'paint-order':'stroke',stroke:'#fff','stroke-width':3},`${meters} m`));}
  $('map-status').textContent=state.selected ? `已选 ${state.selected.name} · ${formatTime(state.selected.minimum_time,state.zone)}；同时显示前后各最多 2 条，共 ${overlays.length} 条事件。` : '没有符合条件的事件。';
}
function zoomMap(factor, point=[480,280]) {
  if(!currentView)return;
  const next=Math.max(1,Math.min(16,mapZoom*factor));
  const wx=currentView.cx+(point[0]-480)/currentView.scale,wy=currentView.cy+(point[1]-280)/currentView.scale;
  mapFocus=[wx-(point[0]-480)/(currentView.scale*next/mapZoom),wy-(point[1]-280)/(currentView.scale*next/mapZoom)];
  mapZoom=next;drawMap();
}
const mapPoint=e=>{const r=$('map').getBoundingClientRect();return [(e.clientX-r.left)*960/r.width,(e.clientY-r.top)*560/r.height];};
$('zoom-in').addEventListener('click',()=>zoomMap(2));
$('zoom-out').addEventListener('click',()=>zoomMap(.5));
$('zoom-reset').addEventListener('click',()=>{mapZoom=1;mapFocus=null;drawMap();});
$('map').addEventListener('wheel',e=>{e.preventDefault();zoomMap(Math.exp(-Math.max(-100,Math.min(100,e.deltaY))*.005),mapPoint(e));},{passive:false});
$('map').addEventListener('dblclick',e=>{e.preventDefault();zoomMap(2,mapPoint(e));});
document.querySelector('.map-frame').addEventListener('keydown',e=>{
  if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key)&&currentView){e.preventDefault();const delta={ArrowLeft:[-80,0],ArrowRight:[80,0],ArrowUp:[0,-80],ArrowDown:[0,80]}[e.key];mapFocus=[currentView.cx+delta[0]/currentView.scale,currentView.cy+delta[1]/currentView.scale];drawMap();}
  if(['+','=','-','0'].includes(e.key)){e.preventDefault();if(e.key==='0')$('zoom-reset').click();else zoomMap(e.key==='-'?.5:2);}
});
const pointers=new Map();let gesture=null,panFrame=null,mapPreview=false;
function commitMapPreview(){if(mapPreview){drawMap();mapPreview=false;}}
function beginMapGesture(){
  if(!currentView||!pointers.size){gesture=null;return;}
  const points=[...pointers.values()],mid=points.length===1?points[0]:[(points[0][0]+points[1][0])/2,(points[0][1]+points[1][1])/2];
  gesture={mid,cx:currentView.cx,cy:currentView.cy,scale:currentView.scale,zoom:mapZoom,
    distance:points.length>1?Math.hypot(points[0][0]-points[1][0],points[0][1]-points[1][1]):null};
}
$('map').addEventListener('pointerdown',e=>{
  if(e.pointerType==='mouse'&&e.button!==0)return;
  e.preventDefault();commitMapPreview();$('map').setPointerCapture(e.pointerId);pointers.set(e.pointerId,mapPoint(e));beginMapGesture();$('map').classList.add('dragging');
});
$('map').addEventListener('pointermove',e=>{
  if(!pointers.has(e.pointerId)||!gesture)return;
  pointers.set(e.pointerId,mapPoint(e));
  if(panFrame!==null)return;
  panFrame=requestAnimationFrame(()=>{
    panFrame=null;if(!gesture||!pointers.size)return;
    const points=[...pointers.values()],mid=points.length===1?points[0]:[(points[0][0]+points[1][0])/2,(points[0][1]+points[1][1])/2];
    const distance=points.length>1?Math.hypot(points[0][0]-points[1][0],points[0][1]-points[1][1]):null;
    mapZoom=Math.max(1,Math.min(16,gesture.zoom*(distance&&gesture.distance?distance/gesture.distance:1)));
    const scale=gesture.scale*mapZoom/gesture.zoom;
    mapFocus=[gesture.cx+(gesture.mid[0]-480)/gesture.scale-(mid[0]-480)/scale,gesture.cy+(gesture.mid[1]-280)/gesture.scale-(mid[1]-280)/scale];
    const next=mapView(state.campus,state.selected?.visibility,mapZoom,mapFocus),factor=next.scale/currentView.scale;
    const dx=480-480*factor+(currentView.cx-next.cx)*next.scale,dy=280-280*factor+(currentView.cy-next.cy)*next.scale;
    $('map-geography')?.setAttribute('transform',`translate(${dx} ${dy}) scale(${factor})`);mapPreview=true;
  });
});
function finishMapGesture(e){commitMapPreview();pointers.delete(e.pointerId);beginMapGesture();if(!pointers.size)$('map').classList.remove('dragging');}
for(const name of ['pointerup','pointercancel','lostpointercapture'])$('map').addEventListener(name,finishMapGesture);
if('ResizeObserver' in window)new ResizeObserver(()=>{if(state.campus)drawMap();}).observe($('map'));
function mapAssetError(text){if(!state.assetWarnings.includes(text)){state.assetWarnings.push(text);renderNotices();}}
fetch('assets/buildings.geojson').then(r=>{if(!r.ok)throw new Error();return r.json();}).then(data=>{
  if(data.type!=='FeatureCollection'||!Array.isArray(data.features))throw new Error();
  state.buildings=data;if(state.campus)drawMap();
}).catch(()=>{state.assetWarnings.push('校园建筑底图暂不可用；事件和预测区域仍可查看。');renderNotices();});
function renderNotices(){
  const list=dataNotices({manifest:state.manifest,selected:state.selected,backend:state.backend,loadError:state.loadError});
  for(const text of [...new Set(state.assetWarnings)])list.push({level:'warning',text});
  list.sort((a,b)=>['critical','warning','info'].indexOf(a.level)-['critical','warning','info'].indexOf(b.level));
  $('data-notices').replaceChildren(...list.map(item=>{
    const li=node('li',`${{critical:'严重',warning:'注意',info:'信息'}[item.level]}：${item.text||reason(item.code)}`,{'data-severity':item.level});
    return li;
  }));
}
let backendCheckedAt=0;
async function checkBackend(){
  if(Date.now()-backendCheckedAt<5*60000)return;
  backendCheckedAt=Date.now();
  const base='https://api.github.com/repos/beblugger/sjtu-transit-predictor/actions/workflows/backend.yml';
  try{
    const response=await fetch(base,{headers:{Accept:'application/vnd.github+json'},signal:AbortSignal.timeout(8000)});
    if(response.status===404){state.backend={state:'missing'};return;}
    if(!response.ok)throw new Error();
    const workflow=await response.json();state.backend={state:workflow.state};
    if(workflow.state==='active'){
      const runs=await fetch(`${base}/runs?per_page=1`,{headers:{Accept:'application/vnd.github+json'},signal:AbortSignal.timeout(8000)});
      if(!runs.ok)throw new Error();
      const run=(await runs.json()).workflow_runs?.[0];
      if(run&&['failure','timed_out','action_required','startup_failure'].includes(run.conclusion))state.backend.failure=run.conclusion;
    }
  }catch{state.backend={state:'unknown'};}finally{renderNotices();}
}
function weatherEvidence(event){
  if(!event)return [node('p','先选择事件，查看该时刻的天气预报。')];
  const condition=weatherCondition(state.weather,event.minimum_time),w=event.weather;
  const content=[node('strong',`${event.name} · ${condition.label}`)];
  const entries=visibleDetailEntries([
    ['云量',quantity(condition.cloud,'%',0)],['降水',quantity(condition.precipitation,'mm',2)],['天气采样时刻',stamp(condition.time)],
    ['评分结论',WEATHER[w.decision]],
    ['综合评分 / 阈值',w.decision==='unavailable'?null:`${number(w.score,3)} / ${number(w.threshold,3)}`],
    ['P0 / P3 / P12',w.decision==='unavailable'?null:`${number(w.p0_clear,3)} / ${number(w.p3_clear,3)} / ${number(w.p12_clear,3)}`],
    ['各模型评分',w.decision==='unavailable'?null:Object.entries(w.model_scores).map(([name,value])=>`${name}: ${number(value,3)}`).join('；')],
    ['模型分歧 / 运行间变化',w.decision==='unavailable'?null:`${number(w.model_gap,3)} / ${number(w.run_change,3)}`],
    ['天气获取时间',stamp(state.weather?.deterministic?.fetched_at)],['集合获取时间',stamp(w.model_updated_at)],
  ]);
  const fields=node('dl',undefined,{class:'metadata'});for(const [name,value] of entries)fields.append(node('dt',name),node('dd',value));
  content.push(fields);
  return content;
}
function showWeatherHint(anchor){
  const tooltip=$('weather-tooltip');
  tooltip.replaceChildren(...weatherEvidence(state.selected));tooltip.hidden=false;
  const rect=anchor.getBoundingClientRect(),width=Math.min(420,innerWidth-24);
  tooltip.style.width=`${width}px`;tooltip.style.left=`${Math.max(12,Math.min(rect.left,innerWidth-width-12))}px`;
  tooltip.style.top=`${Math.max(12,Math.min(rect.bottom+6,innerHeight-tooltip.offsetHeight-12))}px`;
}
function hideWeatherHint(){$('weather-tooltip').hidden=true;}
document.addEventListener('pointerover',e=>{if(e.pointerType!=='mouse')return;const anchor=e.target.closest('[data-weather-trigger]');if(anchor)showWeatherHint(anchor);});
document.addEventListener('pointerout',e=>{const anchor=e.target.closest('[data-weather-trigger]');if(anchor&&!anchor.contains(e.relatedTarget))hideWeatherHint();});
window.addEventListener('scroll',hideWeatherHint,{passive:true,capture:true});
window.addEventListener('blur',hideWeatherHint);
setInterval(()=>{if(state.manifest)renderNotices();},60000);
$('reload').addEventListener('click', loadSnapshot);
window.addEventListener('hashchange', selectSharedEvent);
for(const field of ['weather','target']){
  const tabs=[...document.querySelectorAll(`[data-${field}]`)];
  for(const tab of tabs){
    tab.addEventListener('click',()=>{state.sharedKey=null;$(field).value=tab.dataset[field];state.page=0;render();});
    tab.addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){
      e.preventDefault();const i=tabs.indexOf(tab),next=e.key==='Home'?0:e.key==='End'?tabs.length-1:(i+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
      tabs[next].click();tabs[next].focus();
    }});
  }
}
$('filters').addEventListener('submit',event=>event.preventDefault());
$('filters').addEventListener('input',()=>{state.sharedKey=null;state.page=0;render();});
for (const button of document.querySelectorAll('[data-hours]')) button.addEventListener('click', () => { state.sharedKey=null;setWindow(Number(button.dataset.hours)); state.page = 0; render(); });
$('all-coverage').addEventListener('click', () => { state.sharedKey=null;setWindow(0, true); state.page = 0; render(); });
$('reset').addEventListener('click', () => { resetFilters(); render(); });
$('previous').addEventListener('click',()=>goToPage(state.page-1));
$('next').addEventListener('click',()=>goToPage(state.page+1));
const jumpPage=()=>{if($('page-jump').checkValidity())goToPage($('page-jump').valueAsNumber-1);};
$('go-page').addEventListener('click',jumpPage);
$('page-jump').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();jumpPage();}});
$('location-form').addEventListener('submit', event => {
  event.preventDefault();stopTracking();
  applyLocation({latitude: $('latitude').valueAsNumber, longitude: $('longitude').valueAsNumber},true);
});
$('clear-location').addEventListener('click', () => {stopTracking();applyLocation(null,true);});
$('locate').addEventListener('click', () => {
  if (!navigator.geolocation) { $('location-status').textContent = '此浏览器不支持定位，请手动输入 WGS84 坐标。'; return; }
  $('location-status').textContent = '正在请求设备位置……';
  const request=++locationRequest;
  navigator.geolocation.getCurrentPosition(position => {
    if(request!==locationRequest)return;
    stopTracking();applyLocation({latitude:position.coords.latitude,longitude:position.coords.longitude},true);
    $('location-status').textContent = `设备定位精度报告：约 ${number(position.coords.accuracy, 0)} m；此精度不是凌星预测误差。位置仅在本机使用。`;
  }, error => { $('location-status').textContent = `定位失败：${error.message}。可手动输入 WGS84 坐标。`; }, {enableHighAccuracy: true, timeout: 15000, maximumAge: 30000});
});
$('share').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(location.href); $('share-status').textContent = '事件链接已复制；数据更新后事件可能改变。'; }
  catch { $('share-status').textContent = `请复制地址栏链接：${location.href}`; }
});
function saveLocal(key,value) {
  try{localStorage.setItem(key,JSON.stringify(value));}catch{$('app-status').textContent='本机存储不可用，设置在关闭后不会保留。';}
}
function saveSettings(){saveLocal(SETTINGS_KEY,settings);}
function applyLocation(value,manual=false) {
  state.location=value;
  if(manual)saveLocal('transit-location-v1',value);
  $('inside-only').disabled=!value;$('max-distance').disabled=!value;
  if(value){$('latitude').value=value.latitude;$('longitude').value=value.longitude;}
  else{$('inside-only').checked=false;$('max-distance').value='';}
  $('location-status').textContent=value?'已设置 WGS84 位置，仅在本机计算距离和附近提醒。':'已清除位置。';
  render();checkNearby();
}
let watchId=null,lastDeviceFix=0,locationRequest=0;
function stopTracking(){
  locationRequest++;
  if(watchId!==null)navigator.geolocation?.clearWatch(watchId);
  watchId=null;settings.followDevice=false;$('follow-device').checked=false;saveSettings();
  $('tracking-status').textContent='设备位置跟随已关闭。';
}
function startTracking(){
  if(watchId!==null)return;
  locationRequest++;
  if(!navigator.geolocation){stopTracking();$('tracking-status').textContent='此设备不支持定位，请手动设置位置。';return;}
  applyLocation(null);
  $('tracking-status').textContent='正在请求设备位置……';
  watchId=navigator.geolocation.watchPosition(position=>{
    if(!settings.followDevice)return;
    lastDeviceFix=Date.now();
    applyLocation({latitude:position.coords.latitude,longitude:position.coords.longitude});
    $('tracking-status').textContent=`正在跟随设备位置，定位精度约 ${number(position.coords.accuracy,0)} 米。`;
  },error=>{
    if(error.code===1){stopTracking();applyLocation(null);}
    $('tracking-status').textContent=`定位失败：${error.message}。请设置手动位置或重试设备定位。`;
  },{enableHighAccuracy:true,timeout:15000,maximumAge:30000});
}
let registrationPromise=null;
if('serviceWorker' in navigator){
  registrationPromise=navigator.serviceWorker.register('tiles-sw.js',{updateViaCache:'none'}).then(async registration=>{
    await Promise.race([navigator.serviceWorker.ready,new Promise((_,reject)=>setTimeout(()=>reject(new Error('启动超时')),10000))]);
    return registration;
  });
  registrationPromise.catch(()=>{$('app-status').textContent='App 离线功能和系统通知暂不可用，请联网后重试。';});
}
let sent={};
try{sent=JSON.parse(localStorage.getItem(SENT_KEY))||{};}catch{}
let checkingNearby=false;
async function checkNearby(){
  if(!settings.nearbyNotifications)return;
  if(!('Notification' in window)||Notification.permission!=='granted'){
    $('notification-status').textContent='未获得系统通知权限，请重新开启通知并在系统中允许。';return;
  }
  if(!state.location){$('notification-status').textContent='通知已开启，等待设置位置或开启设备位置跟随。';return;}
  if(settings.followDevice&&Date.now()-lastDeviceFix>2*60000){$('notification-status').textContent='设备位置已超过 2 分钟未更新，暂停附近提醒，等待新定位。';return;}
  if(checkingNearby||!state.manifest)return;
  const candidates=nearbyEvents(state.events,state.location,state.manifest);
  $('notification-status').textContent=`通知已开启：距可见区 100 米内、未来 10 分钟；当前 ${candidates.length} 条。`;
  if(!candidates.length)return;
  checkingNearby=true;
  try{
    const registration=await registrationPromise;
    if(!registration?.showNotification)throw new Error('当前设备不能发送系统通知');
    for(const {event} of candidates){
      const deliver=async()=>{
        // Recheck position and time after activation, a lock, or another reminder.
        if(!settings.nearbyNotifications||Notification.permission!=='granted'||settings.followDevice&&Date.now()-lastDeviceFix>2*60000)return;
        const nearby=nearbyEvents([event],state.location,state.manifest)[0];
        if(!nearby)return;
        const {instant,distance,approximate}=nearby;
        try{sent={...sent,...JSON.parse(localStorage.getItem(SENT_KEY))};}catch{}
        if(alreadyReminded(sent,event,instant))return;
        const title=`附近凌星：${event.name} · ${TARGETS[event.target]}`;
        await registration.showNotification(title,{
          body:`${formatTime(instant,state.zone)} CST${approximate?'（事件计算点时刻）':'（当地约）'} · 距可见区 ${Math.round(distance)} 米 · ${brightnessLabel(event)}`,
          tag:`transit-${reminderKey(event)}`,icon:new URL('assets/icon-192.png',location.href).href,
          data:{url:notificationURL(event,location.href)},
        });
        if(!settings.nearbyNotifications){
          for(const notification of await registration.getNotifications({tag:`transit-${reminderKey(event)}`}))notification.close();
          return;
        }
        sent[reminderKey(event)]=Date.parse(instant);
        sent=Object.fromEntries(Object.entries(sent).filter(([,time])=>time>Date.now()-24*3600000));
        saveLocal(SENT_KEY,sent);
      };
      if(navigator.locks)await navigator.locks.request('transit-nearby-reminder',deliver);
      else await deliver();
    }
  }catch(error){$('notification-status').textContent=`通知发送失败：${error.message}。`;}finally{checkingNearby=false;}
}
$('auto-update').checked=settings.autoUpdate;
$('auto-update').addEventListener('change',()=>{
  settings.autoUpdate=$('auto-update').checked;saveSettings();
  if(settings.autoUpdate)loadSnapshot();
});
$('nearby-notifications').checked=settings.nearbyNotifications;
$('nearby-notifications').addEventListener('change',async()=>{
  const control=$('nearby-notifications');
  if(!control.checked){
    settings.nearbyNotifications=false;saveSettings();$('notification-status').textContent='附近通知已关闭。';
    try{const registration=await registrationPromise;for(const notification of await registration?.getNotifications()||[])if(notification.tag.startsWith('transit-'))notification.close();}catch{}
    return;
  }
  if(!('Notification' in window)||!registrationPromise){control.checked=false;settings.nearbyNotifications=false;saveSettings();$('notification-status').textContent='当前浏览器不支持系统通知；iPhone / iPad 请先添加到主屏幕。';return;}
  control.disabled=true;
  try{
    const permission=await Notification.requestPermission();
    settings.nearbyNotifications=permission==='granted';control.checked=settings.nearbyNotifications;
    saveSettings();
    if(settings.nearbyNotifications)checkNearby();
    else $('notification-status').textContent='未开启通知。请在系统或浏览器设置中允许，再重新开启。';
  }catch{control.checked=false;settings.nearbyNotifications=false;saveSettings();$('notification-status').textContent='无法请求通知权限，请检查系统设置。';}
  finally{control.disabled=false;}
});
$('follow-device').checked=settings.followDevice;
$('follow-device').addEventListener('change',()=>{
  settings.followDevice=$('follow-device').checked;saveSettings();
  if(settings.followDevice)startTracking();else{
    stopTracking();let manual=null;try{manual=JSON.parse(localStorage.getItem('transit-location-v1'));}catch{}
    applyLocation(manual);
  }
});
try{
  const saved=JSON.parse(localStorage.getItem('transit-location-v1'));
  if(!settings.followDevice&&saved&&present(saved.latitude)&&present(saved.longitude)&&Math.abs(saved.latitude)<=90&&Math.abs(saved.longitude)<=180)applyLocation(saved);
}catch{}
if(settings.followDevice)startTracking();
let installPrompt=null;
window.addEventListener('beforeinstallprompt',event=>{event.preventDefault();installPrompt=event;$('install-app').hidden=false;});
$('install-app').addEventListener('click',async()=>{
  if(!installPrompt)return;
  const prompt=installPrompt;installPrompt=null;$('install-app').hidden=true;
  try{await prompt.prompt();}catch{$('app-status').textContent='请通过浏览器菜单安装 App。';}
});
window.addEventListener('appinstalled',()=>{$('install-app').hidden=true;$('app-status').textContent='已安装，可从主屏幕打开。';});
function resumeApp(){
  if(document.visibilityState==='hidden')return;
  if(settings.autoUpdate&&!state.loading)loadSnapshot();
  else checkNearby();
  registrationPromise?.then(registration=>registration.update()).catch(()=>{});
}
document.addEventListener('visibilitychange',resumeApp);
window.addEventListener('online',resumeApp);
window.addEventListener('pageshow',event=>{if(event.persisted)resumeApp();});
setInterval(()=>{if(settings.autoUpdate&&Date.now()-lastRead>=UPDATE_INTERVAL&&document.visibilityState!=='hidden')loadSnapshot();},30000);
setInterval(checkNearby,REMINDER_INTERVAL);
loadSnapshot();

function drawTransit(e) {
  const chart = $('transit-diagram'), d = e.transit_diagram;
  chart.replaceChildren(svg('title', {id:'transit-title'}, '凌星示意图：天球北朝上，天球东朝左'),
    svg('desc', {id:'transit-description'}, '按事件观测点绘制，箭头表示卫星运动方向。'),
    svg('text', {x:12,y:22}, `方位角 ${number(e.target_azimuth_deg,1)}° · 高度角 ${number(e.target_altitude_deg,1)}°`),
    svg('path', {d:'M180 65V35m-4 7 4-7 4 7',stroke:'#333',fill:'none'}),
    svg('text', {x:192,y:49}, '天球北 N'), svg('text',{x:15,y:145},'东 E'));
  const r=67,cx=180,cy=145;
  chart.append(svg('circle',{cx,cy,r,fill:e.target==='sun'?'#ffdc7d':'#384351',stroke:'#66717c'}));
  if(e.target==='moon' && Number.isFinite(d?.illuminated_fraction)) {
    // Orthographic sphere terminator, rotated toward the projected Sun.
    const k=2*d.illuminated_fraction-1;
    const points=[];
    for(let i=0;i<=64;i++){const a=-Math.PI/2+i*Math.PI/64;points.push([r*Math.cos(a),r*Math.sin(a)]);}
    for(let i=64;i>=0;i--){const a=-Math.PI/2+i*Math.PI/64;points.push([-k*r*Math.cos(a),r*Math.sin(a)]);}
    chart.append(svg('path',{d:points.map(([x,y],i)=>`${i?'L':'M'}${x} ${y}`).join(' ')+'Z',fill:'#e6e9ed',transform:`translate(${cx} ${cy}) rotate(${-d.bright_limb_angle_deg-90})`}));
  }
  if(d?.path_arcsec?.length>=2 && d.radius_arcsec>0) {
    const defs=svg('defs'),marker=svg('marker',{id:'transit-arrow',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:7,markerHeight:7,orient:'auto'});
    marker.append(svg('path',{d:'M0 0L10 5L0 10Z',fill:'#0075b5'}));defs.append(marker);chart.append(defs);
    const scale=r/d.radius_arcsec, p=d.path_arcsec,mid=p[1],delta=[p.at(-1)[0]-p[0][0],p.at(-1)[1]-p[0][1]],length=Math.hypot(...delta);
    if(length>0){const extent=95/scale,ends=[-1,1].map(sign=>[cx-(mid[0]+sign*delta[0]/length*extent)*scale,cy-(mid[1]+sign*delta[1]/length*extent)*scale]);
      chart.append(svg('path',{d:`M${ends[0]}L${ends[1]}`,stroke:'#fff','stroke-width':5}),svg('path',{d:`M${ends[0]}L${ends[1]}`,stroke:'#0075b5','stroke-width':2.5,'marker-end':'url(#transit-arrow)'}));}
    chart.append(svg('circle',{cx:cx-mid[0]*scale,cy:cy-mid[1]*scale,r:3,fill:'#0075b5'}));
  }
  const phase=e.target==='moon' ? Number.isFinite(d?.illuminated_fraction)?`月相：照明 ${(d.illuminated_fraction*100).toFixed(1)}%`:'月相：数据待更新' : '太阳';
  chart.append(svg('text',{x:12,y:239},phase),svg('text',{x:12,y:259},d?'蓝箭头：卫星路径 · 圆点：最小角距':'卫星路径：数据待更新'));
  $('transit-caption').textContent='事件观测点 · 天球北朝上、东朝左；方位角北 0°、东 90°。路径为最小角距附近的直线近似。';
}
