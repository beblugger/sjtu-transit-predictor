#!/usr/bin/env node
// Fresh-context first-view benchmark. Requires Playwright: npm install --no-save playwright
const {chromium} = require('playwright');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const zlib = require('node:zlib');
const os = require('node:os');
const {spawnSync} = require('node:child_process');

const root = path.resolve(__dirname, '..');
const dataDir = path.resolve(process.env.BENCH_DATA || path.join(root, 'dist/data'));
const sourceDir = path.resolve(process.env.BENCH_SOURCE || path.join(root, 'deploy'));
const format = process.env.BENCH_FORMAT || 'columns';
const repetitions = Number(process.env.BENCH_RUNS || 3);
const online = process.env.BENCH_URL;
const profiles = (process.env.BENCH_PROFILES || 'unthrottled,measured,slow4g').split(',');
const stage = process.env.BENCH_STAGE || 'full';
const build = process.env.BENCH_BUILD === '1' || stage === 'firstscreen';
const firstscreenLimit = process.env.BENCH_MAX_FIRSTSCREEN_MS ? Number(process.env.BENCH_MAX_FIRSTSCREEN_MS) : null;
const cpuRate = Number(process.env.BENCH_CPU_RATE || 1);
const idleMs = Number(process.env.BENCH_IDLE_MS || (stage === 'firstscreen' ? 1000 : 0));
const minimumRows = Number(process.env.BENCH_MIN_FIRSTSCREEN_ROWS || 1);
const forbidFull = process.env.BENCH_FORBID_FULL === '1' || stage === 'firstscreen';
const fullSnapshotPath = /\/data\/events(?:\.columns)?\.json(?:\.gz)?$/;
const settings = {
  unthrottled: null,
  // Example calibration from the recorded transfer test, not a Pages-wide limit.
  measured: {latency: 150, downloadThroughput: 3.3e6 / 8, uploadThroughput: 750e3 / 8, offline: false},
  slow4g: {latency: 150, downloadThroughput: 1.6e6 / 8, uploadThroughput: 750e3 / 8, offline: false},
};
if (!Number.isInteger(repetitions) || repetitions < 1 || profiles.some(p => !(p in settings)) || !['columns', 'gzip', 'json'].includes(format)) throw Error('Invalid BENCH_RUNS, BENCH_PROFILES or BENCH_FORMAT');
if (!['firstscreen', 'full'].includes(stage) || !Number.isFinite(cpuRate) || cpuRate < 1 || !Number.isFinite(idleMs) || idleMs < 0 || !Number.isInteger(minimumRows) || minimumRows < 0 || (firstscreenLimit !== null && (!Number.isFinite(firstscreenLimit) || firstscreenLimit <= 0))) throw Error('Invalid first-screen benchmark settings');
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
let server, browser, temporaryBuild;
async function main() {
  let url = online;
  let clockTime = process.env.BENCH_NOW;
  if (!url) {
    let servingDir = sourceDir, buildInfo = null;
    const manifest = JSON.parse(fs.readFileSync(path.join(dataDir, 'manifest.json')));
    clockTime ||= manifest.generated_at;
    const files = {};
    for (const name of ['events.json', 'campus.geojson', 'weather.json']) {
      files[name] = fs.readFileSync(path.join(dataDir, name));
      if (hash(files[name]) !== manifest.files[name]) throw Error(`${name} hash mismatch`);
    }
    // Keep the scientific snapshot identical across source versions. Generate only
    // the selected browser representation, without changing the files on disk.
    delete manifest.files['events.json.gz'];
    delete manifest.files['events.columns.json.gz'];
    if (format !== 'json') {
      files['events.json.gz'] = zlib.gzipSync(files['events.json'], {level: 9, mtime: 0});
      manifest.files['events.json.gz'] = hash(files['events.json.gz']);
    }
    if (format === 'columns') {
      const columnar = spawnSync(process.env.PYTHON || 'python3', ['-m', 'app.event_columns'], {
        cwd: root, input: files['events.json'], maxBuffer: 100 * 1024 * 1024,
      });
      if (columnar.error || columnar.status !== 0) throw Error(`Column fixture failed: ${columnar.error || columnar.stderr}`);
      files['events.columns.json.gz'] = columnar.stdout;
      manifest.files['events.columns.json.gz'] = hash(columnar.stdout);
    }
    if (build) {
      const first = spawnSync(process.env.PYTHON || 'python3', ['-m', 'app.first_screen'], {
        cwd:root, input:JSON.stringify({payload:JSON.parse(files['events.json']), weather:JSON.parse(files['weather.json']), campus:JSON.parse(files['campus.geojson']), manifest}), maxBuffer:100*1024*1024,
      });
      if (first.error || first.status !== 0) throw Error(`First-screen fixture failed: ${first.error || first.stderr}`);
      for (const [name, contents] of Object.entries(JSON.parse(first.stdout))) {
        if (path.basename(name) !== name || !/^first-screen(?:-[a-f0-9]+-\d+)?\.json$/.test(name)) throw Error(`Invalid first-screen filename: ${name}`);
        files[name] = Buffer.from(contents);
      }
      manifest.files['first-screen.json'] = hash(files['first-screen.json']);
    }
    files['manifest.json'] = Buffer.from(JSON.stringify(manifest));
    if (build) {
      temporaryBuild = fs.mkdtempSync(path.join(os.tmpdir(), 'transit-bench-build-'));
      const buildData = path.join(temporaryBuild, 'data');
      fs.mkdirSync(buildData);
      for (const [name, bytes] of Object.entries(files)) fs.writeFileSync(path.join(buildData, name), bytes);
      servingDir = path.join(temporaryBuild, 'site');
      const output = spawnSync(process.env.PYTHON || 'python3', [path.join(root, 'scripts/build_frontend.py'), '--source', sourceDir, '--data', buildData, '--output', servingDir], {cwd:root, maxBuffer:1024*1024});
      if (output.error || output.status !== 0) throw Error(`Frontend build failed: ${output.error || output.stderr}`);
      buildInfo = JSON.parse(output.stdout);
      const html = fs.readFileSync(path.join(servingDir, 'index.html'));
      Object.assign(buildInfo, {html_sha256:hash(html), html_gzip_bytes:zlib.gzipSync(html).length});
    }
    const textExtensions = ['.html','.css','.mjs','.js','.json','.geojson','.webmanifest'];
    const compressed = new Map(Object.entries(files).filter(([name]) => textExtensions.includes(path.extname(name))).map(([name, bytes]) => [`data/${name}`, zlib.gzipSync(bytes)]));
    compressed.set('index.html', zlib.gzipSync(fs.readFileSync(path.join(servingDir, 'index.html'))));
    server = http.createServer((req, res) => {
      const pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
      const relative = pathname.replace(/^\/+/, '') || 'index.html';
      let bytes, ext;
      try {
        if (relative.startsWith('data/')) bytes = files[relative.slice(5)];
        else {
          const target = path.resolve(servingDir, relative);
          if (!target.startsWith(servingDir + path.sep)) throw Error('bad path');
          bytes = fs.readFileSync(target);
        }
        if (!bytes) throw Error('missing');
        ext = path.extname(relative);
        const contentType = {'.html':'text/html', '.css':'text/css', '.mjs':'text/javascript', '.js':'text/javascript', '.json':'application/json', '.geojson':'application/geo+json', '.webmanifest':'application/manifest+json', '.gz':'application/gzip', '.png':'image/png'}[ext] || 'application/octet-stream';
        const headers = {'Content-Type':contentType, 'Cache-Control':'no-store'};
        // Pages sends gzip for text when accepted. The .gz artifact itself is not HTTP-compressed.
        if (/\bgzip\b/.test(req.headers['accept-encoding'] || '') && textExtensions.includes(ext)) {
          if (!compressed.has(relative)) compressed.set(relative, zlib.gzipSync(bytes));
          bytes = compressed.get(relative); headers['Content-Encoding'] = 'gzip';
        }
        res.writeHead(200, {...headers, 'Content-Length':bytes.length}); res.end(bytes);
      } catch { res.writeHead(404); res.end(); }
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    url = `http://127.0.0.1:${server.address().port}/`;
    const sourceHashes = Object.fromEntries(fs.readdirSync(sourceDir).filter(name => /\.(?:html|css|mjs|js)$/.test(name)).sort().map(name => [name, hash(fs.readFileSync(path.join(sourceDir, name)))]));
    console.log(JSON.stringify({fixture: dataDir, source: sourceDir, build:buildInfo, label: process.env.BENCH_LABEL || null, format, generated_at: manifest.generated_at, events:manifest.event_count, events_json_sha256:manifest.files['events.json'], events_json_bytes:files['events.json'].length, events_gzip_bytes:files['events.json.gz']?.length, events_columns_gzip_bytes:files['events.columns.json.gz']?.length, first_screen_index_bytes:files['first-screen.json']?.length, source_sha256:sourceHashes}));
  }
  if (clockTime && !Number.isFinite(Date.parse(clockTime))) throw Error('Invalid BENCH_NOW');
  if (process.env.BENCH_SERVE_ONLY === '1') {
    if (!server) throw Error('BENCH_SERVE_ONLY requires local data');
    console.log(JSON.stringify({serving:url, clock:clockTime, format}));
    await new Promise(resolve => {process.once('SIGINT', resolve); process.once('SIGTERM', resolve);});
    return;
  }
  browser = await chromium.launch({headless:true, executablePath: process.env.CHROME_PATH || undefined, args:['--no-sandbox']});
  console.log(JSON.stringify({url, browser:browser.version(), repetitions, profiles, stage, firstscreen_limit_ms:firstscreenLimit, idle_ms:idleMs, external_requests:'blocked', viewport:'1280x720', cpu_rate:cpuRate, clock:clockTime || 'real', service_workers:'blocked'}));
  const results = [];
  for (const profile of profiles) for (let i=0;i<repetitions;i++) {
    const context = await browser.newContext({viewport:{width:1280,height:720}, serviceWorkers:'block'});
    const page = await context.newPage();
    const cdp = await context.newCDPSession(page);
    await cdp.send('Network.enable');
    await cdp.send('Network.setCacheDisabled', {cacheDisabled:true});
    if (cpuRate !== 1) await cdp.send('Emulation.setCPUThrottlingRate', {rate:cpuRate});
    if (settings[profile]) await cdp.send('Network.emulateNetworkConditions', settings[profile]);
    const errors = [], failed = [], requests = [];
    let navigationStarted = null;
    page.on('pageerror', e => errors.push(e.message));
    page.on('request', request => {
      const requestURL = new URL(request.url());
      if (requestURL.origin === new URL(url).origin) requests.push({name:requestURL.pathname, type:request.resourceType(), start:Math.round(performance.now() - navigationStarted), full_snapshot:fullSnapshotPath.test(requestURL.pathname)});
    });
    page.on('requestfailed', r => {if (new URL(r.url()).origin === new URL(url).origin) failed.push(`${r.url()} ${r.failure()?.errorText}`);});
    await page.route('**/*', route => new URL(route.request().url()).origin === new URL(url).origin ? route.continue() : route.abort());
    if (clockTime) await page.addInitScript(instant => {
      // Advance wall time normally from the snapshot date; performance.now stays
      // real. Expired fixtures must still exercise event rows and map overlays.
      const RealDate = Date, base = RealDate.now(), frozen = RealDate.parse(instant);
      window.Date = class extends RealDate {
        constructor(...args) { super(...(args.length ? args : [frozen + RealDate.now() - base])); }
        static now() { return frozen + RealDate.now() - base; }
      };
    }, clockTime);
    await page.addInitScript(() => {
      window.__bench = {map:null, ready:null, settled:null, firstscreen:null, firstscreen_dom:null, fullready:null, firstscreen_state:null, error:null};
      new PerformanceObserver(list => {
        for (const entry of list.getEntries()) {
          if (entry.name === 'transit:fullready' && window.__bench.fullready === null) window.__bench.fullready = entry.startTime;
          if (entry.name !== 'transit:firstscreen' || window.__bench.firstscreen_dom !== null) continue;
          window.__bench.firstscreen_dom = entry.startTime;
          // Two frames include a paint of the populated DOM, rather than merely
          // an application marker that could precede a skeleton's replacement.
          requestAnimationFrame(() => requestAnimationFrame(() => {
            window.__bench.firstscreen = performance.now();
            window.__bench.firstscreen_state = {
              application_visible:!document.querySelector('#application')?.hidden,
              snapshot:document.querySelector('#application')?.dataset.snapshot || null,
              event_rows:document.querySelectorAll('#events [data-event-key]').length,
              geometry:!!document.querySelector('#map path[d]'),
              selected_geometry:!!document.querySelector('#map .visibility-region[data-selected="true"][d]'),
              detail_visible:!!document.querySelector('#detail-content') && !document.querySelector('#detail-content').hidden,
              detail_key:document.querySelector('#detail-content')?.dataset.eventKey || null,
              selected_key:document.querySelector('#events [data-event-key][aria-selected="true"]')?.dataset.eventKey || null,
              detail:document.querySelector('#detail-name')?.textContent || '',
            };
          }));
        }
      }).observe({type:'mark', buffered:true});
      new MutationObserver(() => {
        const map = document.querySelector('#map-status'), status = document.querySelector('#load-status');
        if (window.__bench.map === null && map?.textContent && document.querySelector('#map path[d]') && !document.querySelector('#application')?.hidden) window.__bench.map = performance.now();
        if (window.__bench.ready === null && status?.textContent.includes('数据校验通过')) window.__bench.ready = performance.now();
        if (window.__bench.ready !== null && window.__bench.settled === null && !document.querySelector('#reload')?.disabled) window.__bench.settled = performance.now();
        if (status?.dataset.error === 'true') window.__bench.error = status.textContent;
      }).observe(document, {subtree:true, childList:true, characterData:true, attributes:true});
    });
    const result = {profile, run:i+1};
    try {
      navigationStarted = performance.now();
      await page.goto(url, {waitUntil:'commit', timeout:60000});
      if (stage === 'full' && process.env.BENCH_FULL_TRIGGER) {
        await page.waitForFunction(() => window.__bench && (window.__bench.firstscreen !== null || window.__bench.error), null, {timeout:60000});
        await page.locator(process.env.BENCH_FULL_TRIGGER).click();
      }
      await page.waitForFunction(target => window.__bench && (window.__bench.error || (target === 'firstscreen' ? window.__bench.firstscreen !== null : window.__bench.firstscreen_dom !== null ? window.__bench.fullready !== null : window.__bench.settled !== null)), stage, {timeout:60000});
      Object.assign(result, await page.evaluate(() => {
        const navigation = performance.getEntriesByType('navigation')[0];
        const resources = performance.getEntriesByType('resource').filter(entry => new URL(entry.name).origin === location.origin);
        return {...window.__bench, status:document.querySelector('#load-status')?.textContent, result_count:document.querySelector('#result-count')?.textContent, event_rows:document.querySelectorAll('#events [data-event-key]').length, paint:performance.getEntriesByType('paint').map(e=>({name:e.name,time:e.startTime})), navigation:{response_start:navigation.responseStart, response_end:navigation.responseEnd, encoded:navigation.encodedBodySize, transfer:navigation.transferSize}, firstscreen_encoded_bytes:window.__bench.firstscreen === null ? null : navigation.encodedBodySize + resources.filter(entry => entry.responseEnd <= window.__bench.firstscreen).reduce((sum,entry) => sum + entry.encodedBodySize, 0), resources:resources.map(e=>({name:new URL(e.name).pathname, start:Math.round(e.startTime), duration:Math.round(e.duration), transfer:e.transferSize, encoded:e.encodedBodySize}))};
      }));
      if (result.error || (stage === 'full' ? result.fullready === null && result.ready === null : result.firstscreen === null)) result.failed = true;
      if (stage === 'firstscreen') {
        const state = result.firstscreen_state;
        if (!state.application_visible || !state.geometry || state.event_rows < minimumRows || (state.event_rows && (!state.selected_geometry || !state.detail_visible || !state.selected_key || state.detail_key !== state.selected_key || !state.detail))) throw Error(`First screen is incomplete: ${JSON.stringify(state)}`);
        result.firstscreen_deadline_passed = firstscreenLimit === null || result.firstscreen < firstscreenLimit;
        const visibleRows = page.locator('#events [data-event-key]:visible');
        if (await visibleRows.count() > 1) {
          const before = await page.locator('#events [data-event-key][aria-selected="true"]').getAttribute('data-event-key');
          const candidate = await visibleRows.evaluateAll((rows, selected) => rows.findIndex(row => row.dataset.eventKey !== selected), before);
          const target = visibleRows.nth(candidate);
          const key = await target.getAttribute('data-event-key');
          // Dispatch immediately after the populated first screen is observed;
          // an idle delay must not hide handlers that are attached too late.
          const interactionStarted = performance.now();
          await target.evaluate(element => element.click());
          await page.waitForFunction(expected => document.querySelector('#events [data-event-key][aria-selected="true"]')?.dataset.eventKey === expected && document.querySelector('#detail-content')?.dataset.eventKey === expected && Array.from(document.querySelectorAll('#map .visibility-region[data-selected="true"]')).some(path => path.dataset.eventKey === expected), key, {timeout:10000});
          result.interaction = {passed:true, duration_ms:performance.now()-interactionStarted, previous_key:before, selected_key:key, detail:await page.locator('#detail-name').textContent()};
        } else result.interaction = {passed:null, reason:'Fewer than two event rows'};
      }
      if (idleMs) await page.waitForTimeout(idleMs);
      if (forbidFull && requests.some(request => request.full_snapshot)) throw Error(`Full snapshot was requested during the first-screen run: ${requests.filter(request=>request.full_snapshot).map(request=>request.name).join(', ')}`);
      if (result.firstscreen_deadline_passed === false) {
        result.deadline_failed = true;
        throw Error(`First screen ${result.firstscreen.toFixed(1)} ms exceeds ${firstscreenLimit} ms`);
      }
    } catch (e) {result.failed = true; result.exception = e.message;}
    result.requests = requests;
    result.errors = errors; result.failedRequests = failed;
    if (result.failed || errors.length || failed.length) process.exitCode = 1;
    console.log(JSON.stringify(result));
    results.push(result);
    await context.close();
  }
  await browser.close();
  browser = null;
  const median = values => {const sorted = values.filter(Number.isFinite).sort((a,b) => a-b), mid = Math.floor(sorted.length/2); return sorted.length ? sorted.length % 2 ? sorted[mid] : (sorted[mid-1]+sorted[mid])/2 : null;};
  for (const profile of profiles) {
    // A missed deadline remains a valid timing sample. Excluding slow samples
    // would make the median look faster precisely when the target is missed.
    const samples = results.filter(result => result.profile === profile && (!result.failed || result.deadline_failed) && !result.errors.length && !result.failedRequests.length);
    const firstscreens = samples.map(result=>result.firstscreen).filter(Number.isFinite);
    console.log(JSON.stringify({summary:profile, successful:samples.filter(result=>!result.failed).length, measured_samples:samples.length, repetitions, deadline_passes:firstscreenLimit === null ? null : samples.filter(result=>result.firstscreen_deadline_passed).length, firstscreen_min_ms:firstscreens.length ? Math.min(...firstscreens) : null, firstscreen_max_ms:firstscreens.length ? Math.max(...firstscreens) : null, median_ms:{map:median(samples.map(r=>r.map)), firstscreen:median(firstscreens), firstscreen_dom:median(samples.map(r=>r.firstscreen_dom)), fullready:median(samples.map(r=>r.fullready)), ready:median(samples.map(r=>r.ready)), settled:median(samples.map(r=>r.settled)), fcp:median(samples.map(r=>r.paint.find(p=>p.name === 'first-contentful-paint')?.time))}, median_firstscreen_encoded_bytes:median(samples.map(r=>r.firstscreen_encoded_bytes))}));
  }
}
main().catch(e => {console.error(e); process.exitCode=1;}).finally(async () => {await browser?.close(); server?.close(); if (temporaryBuild) fs.rmSync(temporaryBuild, {recursive:true, force:true});});
