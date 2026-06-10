import os
from typing import Any, Dict

import requests
from fastapi import FastAPI, HTTPException, Request
from google.protobuf.json_format import ParseDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest


PHOENIX_OTLP_HTTP_ENDPOINT = os.getenv(
    "PHOENIX_OTLP_HTTP_ENDPOINT",
    "http://phoenix:6006/v1/traces",
)
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "5"))

app = FastAPI(title="Discord RAG Bot Trace Emitter")


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/traces")
async def emit_traces(request: Request) -> Dict[str, Any]:
    payload = await request.json()

    try:
        export_request = ParseDict(payload, ExportTraceServiceRequest())
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid OTLP JSON trace payload: {exc}",
        ) from exc

    try:
        response = requests.post(
            PHOENIX_OTLP_HTTP_ENDPOINT,
            data=export_request.SerializeToString(),
            headers={"content-type": "application/x-protobuf"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to reach Phoenix OTLP endpoint: {exc}",
        ) from exc

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "phoenix_status_code": response.status_code,
                "phoenix_response": response.text[:1000],
            },
        )

    span_count = 0
    for resource_span in payload.get("resourceSpans", []):
        for scope_span in resource_span.get("scopeSpans", []):
            span_count += len(scope_span.get("spans", []))

    return {
        "status": "forwarded",
        "phoenix_status_code": response.status_code,
        "span_count": span_count,
    }
