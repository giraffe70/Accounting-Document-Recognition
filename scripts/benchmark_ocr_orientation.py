"""Local-only rotation regression against the frozen baseline's OCR direction."""
import argparse
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch

from PIL import Image


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(Path(__file__).absolute().parents[1]))
    from app.core import ocr_orientation, ocr_policy
    manifest = json.loads(Path(args.manifest).read_text(encoding='utf-8'))
    baseline = {row['page_id']: row for row in json.loads(Path(args.baseline).read_text(encoding='utf-8'))}
    results = []
    rotations = {0: None, 90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}
    with ocr_policy.isolated_policy({'orientation_mode': 'auto'}), patch.object(socket.socket, 'connect', side_effect=RuntimeError('network disabled in local orientation pilot')):
        for page in manifest['pages']:
            reference = baseline[page['id']]['response'].get('data', {}).get('image_transforms', {}).get('rotation')
            for angle, operation in rotations.items():
                image_path = output / (page['id'] + '_' + str(angle) + '.png')
                with Image.open(page['path']) as image:
                    image = image.convert('RGB')
                    if operation is not None:
                        image = image.transpose(operation)
                    image.save(image_path)
                prediction = ocr_orientation.classify(image_path)
                expected = None if reference is None else (reference - angle) % 360
                results.append({'page_id': page['id'], 'input_rotation_cw': angle,
                                'expected_angle': expected, 'reference': 'legacy_ocr_orientation',
                                **prediction, 'correct': prediction['angle'] == expected if expected is not None else None})
    (output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    accepted = [r for r in results if r['accepted']]
    print(json.dumps({'total': len(results), 'accepted': len(accepted),
                      'accepted_errors': sum(r['correct'] is not True for r in accepted),
                      'fallbacks': len(results) - len(accepted)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
