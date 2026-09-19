"""Private application HTTP service. Authentication precedes parsing and inference."""
from __future__ import annotations
import hashlib
import hmac
import os

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from .agents import prepare
from .models import PrepareRequest, IntakeResponse


def authorized(header: str | None) -> bool:
    expected = os.environ.get('ROADLENS_MODAL_SERVICE_TOKEN', '')
    supplied = (header or '').removeprefix('Bearer ') if (header or '').startswith('Bearer ') else ''
    return bool(expected) and hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(), hashlib.sha256(expected.encode()).digest())


def create_api(classify_fn, wake_fn):
    api = FastAPI(title='RoadLens private agent service', version='1.0.0', docs_url=None, redoc_url=None)

    @api.middleware('http')
    async def protect(request: Request, call_next):
        if request.url.path != '/health':
            if not authorized(request.headers.get('authorization')):
                return JSONResponse({'error': 'unauthorized'}, status_code=401, headers={'Cache-Control': 'no-store'})
            length = request.headers.get('content-length')
            if length and (not length.isdigit() or int(length) > 16384):
                return JSONResponse({'error': 'payload_too_large'}, status_code=413)
            # Bound streamed/chunked payloads before FastAPI/Pydantic parses them.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16384:
                    return JSONResponse({'error': 'payload_too_large'}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @api.get('/health')
    async def health():
        return {'service': 'RoadLens', 'status': 'ready'}

    @api.post('/prepare', response_model=IntakeResponse)
    async def prepare_route(body: PrepareRequest):
        try:
            return await prepare(body, classifier=classify_fn)
        except Exception:
            raise HTTPException(status_code=503, detail='intake_unavailable') from None

    @api.post('/wake', status_code=202)
    async def wake():
        # No callback URL or job payload is accepted. Workers read only the fixed
        # Site origin and atomically claim D1 jobs there.
        try:
            call_id = await wake_fn()
            return {'accepted': True, 'call_id': call_id}
        except Exception:
            raise HTTPException(status_code=503, detail='wake_unavailable') from None

    return api
