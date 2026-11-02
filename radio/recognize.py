"""Isolated recognizer: parent kills hung clients without affecting capture."""
import asyncio
import json
import sys


def normalize(payload):
    track = payload.get('track') or {}
    if not track.get('key') or not track.get('title'):
        return dict(state='unknown', raw=payload)
    offsets = [m.get('offset') for m in payload.get('matches', []) if isinstance(m.get('offset'), (int, float))]
    return dict(state='matched', artist=track.get('subtitle', ''), title=track['title'],
                track_key=str(track['key']), offset=offsets[0] if offsets else None, raw=payload)


async def recognize(path):
    from shazamio import Shazam
    return normalize(await asyncio.wait_for(Shazam().recognize(path), timeout=35))


if __name__ == '__main__':
    try:
        print(json.dumps(asyncio.run(recognize(sys.argv[1]))))
    except Exception as exc:
        print(json.dumps({'state': 'error', 'error': f'{type(exc).__name__}: {exc}'[:500]}))
        sys.exit(1)
