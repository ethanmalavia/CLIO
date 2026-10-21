"""
Production entry point for CLIO (gunicorn clio.wsgi:app).

Wraps the Flask app from clio.app with usage guardrails for public hosting.
The routes and their behavior are unchanged; local development still uses
`python -m clio.app`, which does not load this module.

Guardrails (all configurable via env vars):
  MAX_REQUEST_BYTES          largest accepted request body           (default 262144)
  MAX_MESSAGE_CHARS          longest /query message                  (default 4000)
  QUERY_RATE_PER_MINUTE      /query calls per client per minute      (default 6)
  REGION_RATE_PER_MINUTE     /region/select calls per client/minute  (default 6)
  DAILY_QUERY_LIMIT          /query calls per instance per UTC day   (default 200)
  ALLOWED_ORIGINS            comma-separated origins allowed by CORS (default: none,
                             the bundled frontend is same-origin and needs no CORS)
"""
import os
import threading
import time
from datetime import datetime, timezone

from flask import request, jsonify

from clio.app import app

MAX_REQUEST_BYTES      = int(os.getenv("MAX_REQUEST_BYTES", "262144"))
MAX_MESSAGE_CHARS      = int(os.getenv("MAX_MESSAGE_CHARS", "4000"))
QUERY_RATE_PER_MINUTE  = int(os.getenv("QUERY_RATE_PER_MINUTE", "6"))
REGION_RATE_PER_MINUTE = int(os.getenv("REGION_RATE_PER_MINUTE", "6"))
DAILY_QUERY_LIMIT      = int(os.getenv("DAILY_QUERY_LIMIT", "200"))
ALLOWED_ORIGINS        = {o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()}

RATE_LIMITS = {
    "query":         QUERY_RATE_PER_MINUTE,
    "region_select": REGION_RATE_PER_MINUTE,
}

app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES

_lock          = threading.Lock()
_minute_window = 0
_minute_counts = {}     # (endpoint, client) -> calls in the current minute
_day           = ""
_day_queries   = 0


def _client_id() -> str:
    # Cloud Run's front end appends the caller's real IP as the last
    # X-Forwarded-For entry; earlier entries can be set by the client.
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.remote_addr or "unknown"


def _too_many(message: str):
    response = jsonify({"error": message})
    response.status_code = 429
    response.headers["Retry-After"] = "60"
    return response


@app.before_request
def _enforce_limits():
    global _minute_window, _minute_counts, _day, _day_queries

    endpoint = request.endpoint
    if endpoint not in RATE_LIMITS:
        return None

    if endpoint == "query":
        body = request.get_json(silent=True) or {}
        message = body.get("message")
        if isinstance(message, str) and len(message) > MAX_MESSAGE_CHARS:
            return jsonify({"error": f"Message is too long (limit {MAX_MESSAGE_CHARS} characters)."}), 413

    with _lock:
        window = int(time.time() // 60)
        if window != _minute_window:
            _minute_window, _minute_counts = window, {}

        key = (endpoint, _client_id())
        if _minute_counts.get(key, 0) >= RATE_LIMITS[endpoint]:
            return _too_many("Too many requests. Please wait a minute and try again.")

        if endpoint == "query":
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != _day:
                _day, _day_queries = today, 0
            if _day_queries >= DAILY_QUERY_LIMIT:
                return _too_many("CLIO has reached its daily question limit. Please try again tomorrow.")
            _day_queries += 1

        _minute_counts[key] = _minute_counts.get(key, 0) + 1

    return None


@app.after_request
def _hide_internal_errors(response):
    # The routes return str(exception) on 500s; keep details in the logs only.
    if response.status_code >= 500 and response.is_json:
        response.set_data(jsonify({"error": "Internal server error."}).get_data())
    return response


@app.errorhandler(413)
def _request_too_large(_error):
    return jsonify({"error": f"Request is too large (limit {MAX_REQUEST_BYTES} bytes)."}), 413


class _RestrictCors:
    """Drop CORS headers for origins not in ALLOWED_ORIGINS.

    clio.app enables CORS for every origin; this WSGI middleware narrows it
    without touching that module.
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        origin = environ.get("HTTP_ORIGIN", "")

        def _start_response(status, headers, exc_info=None):
            if origin not in ALLOWED_ORIGINS:
                headers = [(k, v) for k, v in headers
                           if not k.lower().startswith("access-control-")]
            return start_response(status, headers, exc_info)

        return self.wsgi_app(environ, _start_response)


app.wsgi_app = _RestrictCors(app.wsgi_app)
