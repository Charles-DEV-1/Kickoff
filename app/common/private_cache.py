import hashlib
import json

from flask import jsonify, make_response, request


def private_json(payload, *, max_bytes=2 * 1024 * 1024):
    """Return a bounded private JSON representation with weak ETag support."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > max_bytes:
        raise ValueError("private response exceeds configured bound")
    # These server clocks are carried separately so an unchanged resource can
    # still revalidate to 304 while clients advance the server-issued cursor.
    etag_payload = dict(payload) if isinstance(payload, dict) else payload
    if isinstance(etag_payload, dict):
        etag_payload.pop("server_time", None)
        etag_payload.pop("next_since", None)
    etag_bytes = json.dumps(etag_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    etag = hashlib.sha256(etag_bytes).hexdigest()
    if request.if_none_match.star_tag or request.if_none_match.contains_weak(etag):
        response = make_response("", 304)
    else:
        response = make_response(jsonify(payload), 200)
    response.set_etag(etag, weak=True)
    response.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
    response.vary.add("Authorization")
    if isinstance(payload, dict):
        if isinstance(payload.get("server_time"), str):
            response.headers["X-Server-Time"] = payload["server_time"]
        if isinstance(payload.get("next_since"), str):
            response.headers["X-Next-Since"] = payload["next_since"]
        if isinstance(payload.get("snapshot_at"), str):
            response.headers["X-Snapshot-At"] = payload["snapshot_at"]
    return response
