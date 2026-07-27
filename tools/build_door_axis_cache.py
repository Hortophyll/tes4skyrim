"""Build the door threshold-axis cache from the CONVERTED door meshes.

The navmesh orients every door quad from this: a door panel is thin THROUGH the
opening and wide ACROSS it, so the collision panel's wide horizontal axis is the
threshold and the thin one is the swing direction.

This reads the shipped (output/) meshes, because that is the geometry the
navmesh and the engine actually use, and their body transforms are already baked
into the shape.  The whole-NIF bounding box CANNOT answer the question: it
includes the door frame/arch, which dwarfs the panel and inverts the result for
nine vanilla door models (AnvilDoorMC01 bbox 98x150 -> "Y", panel 97.9x4.5 ->
"X"), each of which laid its door quad 90 degrees out.

Usage:
    python tools/build_door_axis_cache.py [--plugin Oblivion.esm] [--workers N]

Writes <export>/<plugin>/door_panel_axis_cache.json, which
pgrd_to_navm.load_door_centroids reads.  Models absent from the output tree, or
whose panel is thin in Z (trapdoors/hatches/display cases -- they swing about a
horizontal axis and have no vertical threshold), are omitted; the navmesh skips
non-teleport doors that are missing.
"""

import argparse
import json
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def door_models(door_txt):
    """Normalised model paths ('architecture/anvil/x.nif') of every DOOR base."""
    models = set()
    with open(door_txt, encoding='utf-8', errors='replace') as fh:
        txt = fh.read()
    for rec in txt.split('---RECORD_BEGIN---'):
        m = re.search(r'Model\.MODL=(.+)', rec)
        if not m:
            continue
        s = m.group(1).strip().lower()
        s = s.replace('\\', '/')
        s = re.sub(r'/+', '/', s)
        models.add(s)
    return models


def _classify(args):
    path, key = args
    from asset_convert.collision_extract import (door_panel_axis_from_data,
                                                 read_nif_data)
    try:
        return key, door_panel_axis_from_data(read_nif_data(path))
    except Exception:
        return key, None


# Havok units -> world units.  The shipped (Skyrim) collision is authored at
# 1/10 the Oblivion scale, and bhk shapes are a further x7 from world.
_HAVOK_TO_WORLD = 70.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--plugin', default='Oblivion.esm')
    ap.add_argument('--export-dir', default='export')
    ap.add_argument('--output-dir', default='output')
    ap.add_argument('--workers', type=int, default=None)
    a = ap.parse_args()

    door_txt = os.path.join(a.export_dir, a.plugin, 'DOOR.txt')
    if not os.path.exists(door_txt):
        print(f"DOOR.txt not found: {door_txt}")
        return 1
    root = os.path.join(a.output_dir, a.plugin, 'meshes', 'tes4')

    models = door_models(door_txt)
    jobs = []
    missing = 0
    for mdl in sorted(models):
        p = os.path.join(root, *mdl.split('/'))
        if os.path.exists(p):
            jobs.append((p, 'tes4/' + mdl))
        else:
            missing += 1

    workers = a.workers or max(1, (os.cpu_count() or 2) - 1)
    out = {}
    skipped = 0
    print(f"Classifying {len(jobs)} door meshes ({workers} workers)...")
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for key, res in ex.map(_classify, jobs, chunksize=8):
            if res is None:
                skipped += 1
            else:
                axis, width = res
                # [axis, doorway width in WORLD units] — the navmesh sizes each
                # door quad from this rather than one hardcoded half-width.
                out[key] = [axis, round(width * _HAVOK_TO_WORLD, 2)]

    dest = os.path.join(a.export_dir, a.plugin, 'door_panel_axis_cache.json')
    with open(dest, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, indent=0, sort_keys=True)

    ny = sum(1 for v in out.values() if v[0] == 'Y')
    ws = sorted(v[1] for v in out.values())
    print(f"  models={len(models)} classified={len(out)} "
          f"(threshold Y={ny} X={len(out) - ny}) "
          f"no-threshold={skipped} missing-from-output={missing}")
    if ws:
        print(f"  doorway width: min={ws[0]:.0f} median={ws[len(ws) // 2]:.0f} "
              f"max={ws[-1]:.0f}")
    print(f"  wrote {dest}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
