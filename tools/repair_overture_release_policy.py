"""Apply the release-policy repair after the verified geometry repair.
Current mode captures the exact official release before/after download. Strict
pinned mode remains available as OVERTURE_RELEASE_POLICY=pinned.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    path = ROOT / 'app/providers.py'
    data = path.read_bytes()
    marker = b'def _download_overture_release('
    if marker in data:
        return
    anchor = b'\ndef fetch_overture_footprint('
    if data.count(anchor) != 1:
        raise RuntimeError('Unexpected Overture source layout')
    helper = b'''
def _download_overture_release(settings: Settings) -> str:
    """Audit the official current release, or enforce an explicit strict pin."""
    policy = os.getenv("OVERTURE_RELEASE_POLICY", "current").strip().lower()
    if policy == "pinned":
        return _require_pinned_overture_release(settings)
    if policy != "current":
        raise UnreliableGeometryError(
            "OVERTURE_RELEASE_POLICY_INVALID",
            "OVERTURE_RELEASE_POLICY must be current or pinned.",
        )
    return _current_overture_release(settings)

'''
    data = data.replace(anchor, helper + anchor, 1)
    replacements = [
        (b'    release = _require_pinned_overture_release(settings)',
         b'    release = _download_overture_release(settings)'),
        (b'    if _require_pinned_overture_release(settings) != release:',
         b'    if _download_overture_release(settings) != release:'),
    ]
    for old, new in replacements:
        if data.count(old) != 1:
            raise RuntimeError('Unexpected release-call layout')
        data = data.replace(old, new, 1)
    path.write_bytes(data)


if __name__ == '__main__':
    main()
