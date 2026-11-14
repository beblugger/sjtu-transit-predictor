import {inflateBands} from './bands.mjs';

export async function getBytes(url) {
  const response = await fetch(url, {cache: 'no-store', signal: AbortSignal.timeout(20000)});
  if (!response.ok) throw new Error(`${url.pathname.split('/').at(-1)}：HTTP ${response.status}`);
  return new Uint8Array(await response.arrayBuffer());
}

async function verify(bytes, filename, digest) {
  if (!/^[a-f0-9]{64}$/.test(digest || '')) throw new Error(`${filename} 缺少有效 SHA256`);
  if (!globalThis.crypto?.subtle) throw new Error('当前环境不能校验数据，请使用 HTTPS 或 localhost 打开');
  const actual = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), b => b.toString(16).padStart(2, '0')).join('');
  if (actual !== digest) throw new Error(`${filename} 校验不一致，可能正在更新`);
}

async function checkedBytes(filename, manifest, earlyBytes = null) {
  const digest = manifest.files?.[filename];
  if (!/^[a-f0-9]{64}$/.test(digest || '')) throw new Error(`${filename} 缺少有效 SHA256`);
  const url = new URL(`data/${filename}`, location.href); url.searchParams.set('v', digest);
  const bytes = await Promise.resolve(earlyBytes).catch(()=>null) || await getBytes(url);
  await verify(bytes, filename, digest);
  return bytes;
}

export async function checkedJSON(filename, manifest, earlyBytes = null) {
  return JSON.parse(new TextDecoder().decode(await checkedBytes(filename, manifest, earlyBytes)));
}

// Reconstruct independent JSON values; dictionary entries may contain mutable geometry arrays.
function cloneJSON(value) {
  if (value === null || typeof value !== 'object') return value;
  return typeof structuredClone === 'function' ? structuredClone(value) : JSON.parse(JSON.stringify(value));
}

function decodeColumn(node, count) {
  const invalid = () => { throw new Error('事件列数据格式无效'); };
  if (!node || typeof node !== 'object' || Array.isArray(node)) return invalid();
  if (Object.hasOwn(node, 'o')) {
    if (!node.o || typeof node.o !== 'object' || Array.isArray(node.o) ||
        (node.p !== undefined && (!Array.isArray(node.p) || node.p.length !== count || node.p.some(p => typeof p !== 'boolean')))) return invalid();
    const fields = Object.entries(node.o).map(([key, column]) => [key, decodeColumn(column, count)]);
    return Array.from({length: count}, (_, index) => node.p && !node.p[index] ? null :
      Object.fromEntries(fields.map(([key, values]) => [key, values[index]])));
  }
  if (Array.isArray(node.v) && node.v.length === count) return node.v;
  if (Array.isArray(node.d) && Array.isArray(node.i) && node.i.length === count) {
    return node.i.map(index => {
      if (!Number.isInteger(index) || index < 0 || index >= node.d.length) return invalid();
      return cloneJSON(node.d[index]);
    });
  }
  return invalid();
}

export function decodeColumnSnapshot(document) {
  if (!document || !['event-columns-v1', 'event-columns-v2'].includes(document.format) || !Number.isSafeInteger(document.count) || document.count < 0 ||
      !document.metadata || typeof document.metadata !== 'object' || Array.isArray(document.metadata)) throw new Error('事件列数据格式无效');
  const events = decodeColumn(document.events, document.count);
  if (events.some(event => !event || typeof event !== 'object' || Array.isArray(event))) throw new Error('事件列数据格式无效');
  if (document.format === 'event-columns-v2') for (const event of events) {
    if (typeof event.identifier === 'number') {
      if (!Number.isSafeInteger(event.identifier) || event.identifier < 0) throw new Error('事件列数据格式无效');
      event.identifier = String(event.identifier);
    }
    if (Number.isInteger(event.satellite_azimuth_offset) && Number.isInteger(event.target_azimuth_deg)) {
      event.azimuth_deg = ((event.target_azimuth_deg + event.satellite_azimuth_offset + 3600) % 3600) / 10;
      event.target_azimuth_deg /= 10;
      delete event.satellite_azimuth_offset;
    }
    if (Number.isInteger(event.satellite_altitude_offset) && Number.isInteger(event.target_altitude_deg)) {
      event.altitude_deg = (event.target_altitude_deg + event.satellite_altitude_offset) / 10;
      event.target_altitude_deg /= 10;
      delete event.satellite_altitude_offset;
    }
    inflateBands(event);
  }
  return {...document.metadata, events};
}

export async function checkedEvents(manifest, earlyBytes = null) {
  if ((!manifest.files?.['events.columns.json.gz'] && !manifest.files?.['events.json.gz']) || typeof DecompressionStream === 'undefined') {
    return checkedJSON('events.json', manifest);
  }
  const columnar = Boolean(manifest.files?.['events.columns.json.gz']);
  const filename = columnar ? 'events.columns.json.gz' : 'events.json.gz';
  const prefetched = columnar ? await Promise.resolve(earlyBytes).catch(() => null) : null;
  const compressed = prefetched || await checkedBytes(filename, manifest);
  if (prefetched) await verify(compressed, filename, manifest.files[filename]);
  const stream = new Blob([compressed]).stream().pipeThrough(new DecompressionStream('gzip'));
  const bytes = new Uint8Array(await new Response(stream).arrayBuffer());
  if (columnar) return decodeColumnSnapshot(JSON.parse(new TextDecoder().decode(bytes)));
  await verify(bytes, 'events.json', manifest.files?.['events.json']);
  return JSON.parse(new TextDecoder().decode(bytes));
}
