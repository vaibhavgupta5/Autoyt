"""After upload: look for rejected or removed videos and switch off the source that caused it."""
from __future__ import annotations

import logging
import time

from .config import Settings
from .db import Database
from .notify import notify_once

log = logging.getLogger("autoyt.monitor")
STRIKE_REASONS = {"claim", "copyright", "termsOfUse", "trademark"}


def check_uploaded(s: Settings, db: Database, service, day: str) -> list[str]:
    """Check clips uploaded in the last 14 days. Returns human-readable findings."""
    cutoff = time.time() - 14 * 86400
    rows = db.query("SELECT c.id, c.youtube_id, c.heading, r.source FROM clips c JOIN runs r ON r.day=c.day "
                    "WHERE c.youtube_id IS NOT NULL AND c.status='uploaded' AND c.updated_at>?", (cutoff,))
    if not rows:
        return []
    findings: list[str] = []
    by_id = {r["youtube_id"]: r for r in rows}
    ids = list(by_id)
    seen: set[str] = set()
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        resp = service.videos().list(part="status", id=",".join(batch)).execute()
        for item in resp.get("items", []):
            vid = item["id"]
            seen.add(vid)
            st = item.get("status", {})
            if st.get("uploadStatus") in ("rejected", "failed"):
                reason = st.get("rejectionReason") or st.get("failureReason") or "unknown"
                row = by_id[vid]
                findings.append(f"{vid} '{row['heading']}' ({row['source']}): {st['uploadStatus']} - {reason}")
                db.update_clip(row["id"], status="failed", error=f"{st['uploadStatus']}: {reason}")
                if reason in STRIKE_REASONS and row["source"]:
                    db.disable_source(row["source"], f"{reason} on {vid}")
                    findings.append(f"Source '{row['source']}' is now disabled.")
    for vid in set(ids) - seen:
        row = by_id[vid]
        findings.append(f"{vid} '{row['heading']}' ({row['source']}): no longer on the channel (deleted or removed)")
        db.update_clip(row["id"], status="failed", error="missing from channel")
    if findings:
        notify_once(s, db, day, "monitor",
                    "Action needed: YouTube removed or rejected videos",
                    "\n".join(findings) + "\n\nContent ID claims on a video that stays up are NOT visible to the API. "
                    "Watch YouTube's copyright emails and check Studio > Content > Copyright.")
    return findings
