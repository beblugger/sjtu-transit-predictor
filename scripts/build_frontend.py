"""Prepare the static site with a single-request application shell and first page."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil


IMPORT = re.compile(r"^import\s*\{([^}]+)\}\s*from\s*['\"]\./([^'\"]+)['\"];?", re.MULTILINE)
EXPORT = re.compile(r"\bexport\s+(?:(?:async\s+)?function|const|let|class)\s+([A-Za-z_$][\w$]*)")


def bundle_modules(source: Path, entry: str = "app.mjs") -> str:
    """Bundle this app's local named ES imports without an npm build dependency.

    Each module keeps its own scope. Unsupported syntax and cycles fail the build
    instead of silently producing a partial runtime.
    """
    modules, visiting = [], set()
    complete = set()

    def visit(name):
        if name in complete:
            return
        if name in visiting or Path(name).name != name or not name.endswith('.mjs'):
            raise ValueError(f"Unsupported module dependency: {name}")
        visiting.add(name)
        text = (source / name).read_text()
        imports = list(IMPORT.finditer(text))
        for match in imports:
            visit(match[2])
        exports = EXPORT.findall(text)

        def resolve(match):
            names = re.sub(r"\s+as\s+", ": ", match[1])
            return f"const {{{names}}} = __modules[{json.dumps(match[2])}];"

        text = IMPORT.sub(resolve, text)
        text = re.sub(r"\bexport\s+(?=(?:async\s+)?function\b|const\b|let\b|class\b)", "", text)
        if re.search(r"^\s*(?:import|export)\s", text, re.MULTILINE):
            raise ValueError(f"Unsupported ES module syntax in {name}")
        modules.append(f"__modules[{json.dumps(name)}] = (() => {{\n{text}\nreturn {{{','.join(exports)}}};\n}})();")
        visiting.remove(name)
        complete.add(name)

    visit(entry)
    return "const __modules = Object.create(null);\n" + "\n".join(modules)


def script_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).replace('<', '\\u003c')


def build_frontend(source: Path, data: Path, output: Path) -> dict:
    manifest = json.loads((data / 'manifest.json').read_text())
    index = packet = initial = None
    if 'first-screen.json' in manifest.get('files', {}):
        index_bytes = (data / 'first-screen.json').read_bytes()
        if hashlib.sha256(index_bytes).hexdigest() != manifest['files']['first-screen.json']:
            raise ValueError('First-screen index digest mismatch')
        index = json.loads(index_bytes)
        if index['source_sha256'] != manifest['files']['events.json']:
            raise ValueError('First-screen index belongs to another snapshot')
        # Compare parsed instants, since equivalent UTC timestamps may use Z or +00:00.
        from datetime import datetime
        instant = lambda value: datetime.fromisoformat(value.replace('Z', '+00:00'))
        for entry in index['packets']:
            if Path(entry['filename']).name != entry['filename']:
                raise ValueError('First-screen filenames must be local basenames')
        initial = next((p for p in index['packets'] if instant(p['valid_from']) <= instant(manifest['generated_at']) < instant(p['valid_until'])), None)
        if initial:
            packet_bytes = (data / initial['filename']).read_bytes()
            if hashlib.sha256(packet_bytes).hexdigest() != initial['sha256']:
                raise ValueError('First-screen packet digest mismatch')
            packet = json.loads(packet_bytes)
    runtime = bundle_modules(source)
    html = (source / 'index.html').read_text()
    html = re.sub(r'\s*<link rel="modulepreload"[^>]*>', '', html)
    html = html.replace('<link rel="stylesheet" href="styles.css">', '<style>\n' + (source / 'styles.css').read_text() + '\n</style>')
    html = html.replace('<script type="module" src="app.mjs"></script>', '')
    inline = ''
    if index is not None:
        state = {'index': index}
        early = '<script>window.__firstScreenData=' + script_json(state) + ';\n'
        early += """{
      const now=Date.now(), index=window.__firstScreenData.index;
      const selected=index.packets.find(p=>Date.parse(p.valid_from)<=now&&now<Date.parse(p.valid_until));
      if(selected&&selected.filename!==INITIAL_PACKET&&!new URLSearchParams(location.hash.slice(1)).has('event')) {
        const bytes=fetch(new URL('data/'+selected.filename,location.href),{cache:'no-store',signal:AbortSignal.timeout(20000)})
          .then(r=>r.ok?r.arrayBuffer():null).then(b=>b?new Uint8Array(b):null).catch(()=>null);
        window.__firstScreenPacketRequest={filename:selected.filename,bytes};
      }
    }</script>\n""".replace('INITIAL_PACKET', script_json(initial['filename'] if initial else None))
        html = html.replace('<meta name="theme-color" content="#193f64">', '<meta name="theme-color" content="#193f64">\n' + early)
        inline = '<script>window.__firstScreenData.packet=' + script_json(packet) + ';</script>\n'
    inline += '<script type="module">\n' + runtime.replace('</script', '<\\/script') + '\n</script>\n'
    html = html.replace('</body>', inline + '</body>')
    output.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if path.is_file():
            shutil.copy2(path, output / path.name)
        elif path.is_dir():
            shutil.copytree(path, output / path.name, dirs_exist_ok=True)
    (output / 'index.html').write_text(html)
    return {'html_bytes': len(html.encode()), 'inline_events': len(packet['payload']['events']) if packet else 0,
            'packets': len(index['packets']) if index else 0, 'embedded_first_screen': index is not None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('deploy'))
    parser.add_argument('--data', type=Path, default=Path('dist/data'))
    parser.add_argument('--output', type=Path, default=Path('dist'))
    args = parser.parse_args()
    print(json.dumps(build_frontend(args.source, args.data, args.output)))


if __name__ == '__main__':
    main()
