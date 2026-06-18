#!/usr/bin/env python
"""One-shot R2 round-trip probe for the S22 Group-D botocore.Config -> AioConfig swap.

Mocked unit tests never construct a real R2 client, so this is the only proof that the
AioConfig path (request signing + endpoint + header serialization) is intact end-to-end.

It exercises the ACTUAL shipped code paths (no client reconstruction):
  - PUT via ``runner._upload_html_to_r2`` (the canonical-key write path).
  - GET back via ``EdgarClient`` entered as ``async with``, reaching its ``__aenter__``-built
    ``_r2`` client directly (EdgarClient embeds put/get inside ``fetch_primary_doc`` and exposes
    no generic getter; reaching the entered client is acceptable for a throwaway probe).

Why assert on ContentType, not just a 200: AioConfig governs signing and header serialization.
A 200 only proves the request was accepted; the metadata surviving PUT -> GET proves the signing
and header path together. So we assert the body bytes AND the ContentType round-trip.

Cleanup note: there is NO object-expiration lifecycle rule on the bucket (only
AbortIncompleteMultipartUpload, see scripts/setup_r2_lifecycle.sh). A leftover probe object would
persist indefinitely, so delete_object is mandatory cleanup here, not a nicety. If delete fails
(e.g. the token lacks DeleteObject), we warn loudly so the object can be removed by hand.

Throwaway diagnostic. Run: uv run python scripts/validate_aioconfig.py
Exit 0 = healthy, 1 = failure.
"""

from __future__ import annotations

import asyncio
import sys
import time
import traceback

from dotenv import load_dotenv

from alphalens.config import get_settings
from alphalens.etl import runner
from alphalens.etl.edgar import EdgarClient

_BODY = b"<html><body>aioconfig round-trip probe</body></html>"
_CONTENT_TYPE = "text/html"


async def _probe() -> int:
    settings = get_settings()
    key = f"tmp/aioconfig_probe_{int(time.time())}.html"
    bucket = settings.r2_bucket_name

    print(f"[probe] bucket={bucket} endpoint={settings.r2_endpoint_url}")
    print(f"[probe] key={key}")

    # PUT via the shipped canonical-key path (builds its own AioConfig client internally).
    await runner._upload_html_to_r2(key, _BODY, settings=settings)
    print(f"[probe] PUT ok ({len(_BODY)} bytes)")

    # GET back through EdgarClient's __aenter__-built client.
    async with EdgarClient(settings) as edgar:
        assert edgar._r2 is not None, "EdgarClient._r2 not initialized by __aenter__"
        r2 = edgar._r2
        resp = await r2.get_object(Bucket=bucket, Key=key)
        got = bytes(await resp["Body"].read())
        got_ct = resp.get("ContentType")
        print(f"[probe] GET ok ({len(got)} bytes, ContentType={got_ct!r})")

        ok = True
        if got != _BODY:
            print(f"[FAIL] body mismatch: expected {_BODY!r}, got {got!r}")
            ok = False
        if got_ct != _CONTENT_TYPE:
            print(f"[FAIL] ContentType mismatch: expected {_CONTENT_TYPE!r}, got {got_ct!r}")
            ok = False

        # Cleanup is mandatory — no expiration lifecycle rule exists on this bucket.
        try:
            await r2.delete_object(Bucket=bucket, Key=key)
            print(f"[probe] cleanup ok (deleted {key})")
        except Exception as exc:  # cleanup-only failure must NOT fail the probe
            print(
                f"[WARN] delete_object failed ({exc!r}). The bucket has NO object-expiration "
                f"lifecycle rule, so {key} will persist indefinitely — delete it manually."
            )

    if ok:
        print("[probe] HEALTHY: AioConfig PUT->GET round-trip + ContentType intact")
        return 0
    print("[probe] FAILED: round-trip assertions did not hold")
    return 1


def main() -> int:
    load_dotenv()
    try:
        return asyncio.run(_probe())
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
