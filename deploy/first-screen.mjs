import {checkedJSON, getBytes} from './data.mjs';
import {inflateBands} from './bands.mjs';
import {validateSnapshot} from './core.mjs';

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const digest = value => /^[a-f0-9]{64}$/.test(value || '');
const invalid = () => { throw new Error('首屏数据格式或版本无效'); };
const clone = value => typeof structuredClone === 'function' ? structuredClone(value) : JSON.parse(JSON.stringify(value));
const packetCache = new Map();
let embeddedData, latestIndex;

// The build embeds the small index and its current packet in the document.
// As events expire, read exactly one verified replacement packet; the complete
// event catalogue and full weather snapshot are left for explicit later use.
export async function loadFirstScreen({refresh = false, now = Date.now()} = {}) {
  const initial = globalThis.__firstScreenData;
  if (initial !== embeddedData) { embeddedData = initial; latestIndex = null; }
  if ((!initial && !latestIndex && !refresh) ||
      (!latestIndex && new URLSearchParams(location.hash.slice(1)).has('event'))) return null;
  let index = latestIndex || initial?.index, manifest;
  if (refresh) {
    const url = new URL('data/manifest.json', location.href);
    url.searchParams.set('read', String(Date.now()));
    manifest = JSON.parse(new TextDecoder().decode(await getBytes(url)));
    if (!manifest.files?.['first-screen.json']) return null;
    index = await checkedJSON('first-screen.json', manifest);
  }
  if (!object(index) || index.format !== 'first-screen-index-v1' || !digest(index.source_sha256) ||
      !Number.isFinite(Date.parse(index.generated_at)) || !Number.isFinite(Date.parse(index.from_time)) ||
      !Number.isFinite(Date.parse(index.to_time)) || Date.parse(index.from_time) >= Date.parse(index.to_time) ||
      !Array.isArray(index.packets) || index.packets.some(entry => !object(entry) ||
        !/^first-screen-[a-f0-9]{12}-\d{5}\.json$/.test(entry.filename || '') || !digest(entry.sha256) ||
        !Number.isSafeInteger(entry.count) || entry.count < 0 || !Number.isFinite(Date.parse(entry.valid_from)) ||
        !Number.isFinite(Date.parse(entry.valid_until)) || Date.parse(entry.valid_from) >= Date.parse(entry.valid_until)) ||
      (manifest && (manifest.files?.['events.json'] !== index.source_sha256 || manifest.generated_at !== index.generated_at ||
        manifest.from_time !== index.from_time || manifest.to_time !== index.to_time))) invalid();
  const entry = index.packets.find(packet => Date.parse(packet.valid_from) <= now && now < Date.parse(packet.valid_until));
  if (!entry) return null;
  const embedded = initial?.packet, cacheKey = `${entry.filename}:${entry.sha256}`;
  const packet = packetCache.has(cacheKey) ? clone(packetCache.get(cacheKey)) : object(embedded) && embedded.source_sha256 === index.source_sha256 &&
    embedded.valid_from === entry.valid_from && embedded.valid_until === entry.valid_until ? clone(embedded) :
    await checkedJSON(entry.filename, {files: {[entry.filename]: entry.sha256}},
      globalThis.__firstScreenPacketRequest?.filename === entry.filename ? globalThis.__firstScreenPacketRequest.bytes : null);
  if (!object(packet) || packet.format !== 'first-screen-v1' || packet.source_sha256 !== index.source_sha256 ||
      packet.valid_from !== entry.valid_from || packet.valid_until !== entry.valid_until ||
      packet.default_event_count !== entry.count || !object(packet.manifest) || !object(packet.payload) ||
      packet.manifest.files?.['events.json'] !== index.source_sha256 || packet.manifest.generated_at !== index.generated_at ||
      packet.manifest.from_time !== index.from_time || packet.manifest.to_time !== index.to_time ||
      !Number.isSafeInteger(packet.manifest.event_count) || packet.manifest.event_count < entry.count ||
      !Array.isArray(packet.payload.events) || packet.payload.events.length !== Math.min(10, entry.count) ||
      packet.payload.event_count !== packet.payload.events.length || !object(packet.weather)) invalid();
  const source = clone(packet);
  for (const event of packet.payload.events) {
    if (!object(event)) invalid();
    inflateBands(event);
  }
  validateSnapshot({...packet.manifest, event_count: packet.payload.events.length}, packet.payload, packet.campus);
  packetCache.set(cacheKey, source);
  while (packetCache.size > 8) packetCache.delete(packetCache.keys().next().value);
  latestIndex = index;
  return packet;
}
