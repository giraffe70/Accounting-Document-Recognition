"""Explicit, manifest-bound OCR/VLM pilot; never runs during offline verification.

Run once per frozen source tree. Each phase uses a fresh process, matching the
validation service, while the page's disk cache is shared by its phases.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def write_envelope(manifest, output, label):
    results = json.loads((output / 'results.json').read_text(encoding='utf-8'))
    for row in results:
        trace = row['response'].get('data', {}).get('trace', [])
        snapshots = [event for event in trace if event.get('kind') == 'snapshot']
        if not snapshots or any(event.get('parameters', {}).get('run_id') != manifest[label + '_run_id']
                                or event.get('parameters', {}).get('combo') != manifest['combo'] for event in snapshots):
            raise ValueError('Actual saved snapshot does not match pilot run/model identity')
    envelope = {'run_id': manifest[label + '_run_id'], 'combo': manifest['combo'], 'results': results}
    if label == 'baseline':
        envelope['frozen_commit'] = manifest['baseline_commit']
    destination = output / 'envelope.json'
    if destination.exists():
        raise ValueError('Existing envelope must not be overwritten')
    destination.write_text(json.dumps(envelope, ensure_ascii=False, default=str), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--source-root', required=True)
    parser.add_argument('--data-root', required=True)
    parser.add_argument('--label', required=True, choices=('baseline', 'candidate'))
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--phase-payload')
    parser.add_argument('--wrap-existing', action='store_true')
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.source_root).absolute()))
    from app import config
    config.DATA_DIR = Path(args.data_root).absolute()
    config.SETTINGS_FILE = config.DATA_DIR / 'settings.json'
    from app import validation
    manifest = json.loads(Path(args.manifest).read_text(encoding='utf-8'))
    output = Path(args.output_dir).absolute()
    if not args.phase_payload and not args.wrap_existing and output.exists():
        raise ValueError('Output directory must be new; existing pilot results are never overwritten')
    output.mkdir(parents=True, exist_ok=True)
    if args.wrap_existing:
        write_envelope(manifest, output, args.label)
        return
    if args.phase_payload:
        payload = json.loads(Path(args.phase_payload).read_text(encoding='utf-8'))
        data = validation._recognize(payload)
        (output / 'response.json').write_text(json.dumps(data, ensure_ascii=False, default=str), encoding='utf-8')
        return
    gold = Path(manifest['gold_path'])
    if hashlib.sha256(gold.read_bytes()).hexdigest() != manifest['gold_sha256']:
        raise ValueError('Frozen gold hash changed')
    if any(str(manifest['combo'].get(key, '')).startswith('ollama/') for key in ('structure_model', 'classify_model')):
        import requests
        from app.services import sysconfig
        endpoint = sysconfig.get('llm', 'ollama_base_url') or config.OLLAMA_BASE_URL
        requests.get(endpoint.rstrip('/') + '/api/version', timeout=5).raise_for_status()
    results = []
    for number, page in enumerate(manifest['pages'], 1):
        if hashlib.sha256(Path(page['path']).read_bytes()).hexdigest() != page['sha256']:
            raise ValueError('Frozen image hash changed')
        result_id = hashlib.sha256((args.label + page['id']).encode()).hexdigest()[:32]
        payload = {'run_id': manifest[args.label + '_run_id'], 'id': result_id,
                   'page_path': page['path'], 'relative_path': page['source'],
                   'page_no': page['page'], 'max_edge': 1180, 'combo': manifest['combo']}
        calls = 0
        def runner(part, timeout):
            nonlocal calls
            calls += 1
            folder = output / result_id / str(calls)
            folder.mkdir(parents=True, exist_ok=True)
            request = folder / 'payload.json'
            request.write_text(json.dumps(part, ensure_ascii=False), encoding='utf-8')
            command = [sys.executable, '-B', '-X', 'utf8', str(Path(__file__).absolute()),
                       '--manifest', args.manifest, '--source-root', args.source_root,
                       '--data-root', args.data_root, '--label', args.label,
                       '--output-dir', str(folder), '--phase-payload', str(request)]
            started = time.perf_counter()
            with (folder / 'worker.log').open('w', encoding='utf-8') as log:
                try:
                    completed = subprocess.run(command, stdout=log, stderr=log, timeout=timeout,
                                               env={**os.environ, 'PYTHONUTF8': '1'})
                    if completed.returncode:
                        return {'ok': False, 'error': 'worker failed; see worker.log',
                                'wall_seconds': time.perf_counter() - started}
                    return {'ok': True, 'data': json.loads((folder / 'response.json').read_text(encoding='utf-8')),
                            'wall_seconds': time.perf_counter() - started}
                except subprocess.TimeoutExpired:
                    return {'ok': False, 'timeout': True, 'error': 'pilot phase timeout',
                            'wall_seconds': time.perf_counter() - started}
        started = time.perf_counter()
        response = validation._run_page_child(payload, 900, runner=runner)
        results.append({'page_id': page['id'], 'source': page['source'], 'page': page['page'],
                        'image_sha256': page['sha256'], 'wall_seconds': time.perf_counter() - started,
                        'response': response})
        (output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, default=str), encoding='utf-8')
        print(f'{args.label} {number}/{len(manifest["pages"])}: ok={response.get("ok")} seconds={results[-1]["wall_seconds"]:.1f}', flush=True)
    if hashlib.sha256(gold.read_bytes()).hexdigest() != manifest['gold_sha256']:
        raise ValueError('Gold changed during pilot')
    write_envelope(manifest, output, args.label)


if __name__ == '__main__':
    main()
