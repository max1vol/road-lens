"""Lease-aware D1 outbox consumer; the Site is the only mutable-state authority."""
from __future__ import annotations
import asyncio
from contextlib import suppress
import logging
import os
from urllib.parse import urlsplit

import httpx
from pydantic import Field
from .agents import analyze
from .models import StrictModel, Id, ReadyDraft, JobResult
from typing import Literal

log = logging.getLogger('roadlens.worker')

class JobPayload(StrictModel):
    question: str | None = Field(default=None, max_length=2000)
    report: ReadyDraft | None = None

class ClaimedJob(StrictModel):
    id: Id
    kind: Literal['report_analysis', 'officer_question']
    payload: JobPayload
    lease_token: Id
    attempts: int = Field(ge=1, le=3)
    lease_expires: str | None = None

class ClaimResponse(StrictModel):
    job: ClaimedJob | None

class LeaseLost(Exception):
    pass


def site_origin() -> str:
    value = os.environ['ROADLENS_SITE_ORIGIN'].rstrip('/')
    parts = urlsplit(value)
    if parts.scheme != 'https' or not parts.netloc or parts.username or parts.password or parts.path or parts.query or parts.fragment:
        raise ValueError('ROADLENS_SITE_ORIGIN must be a fixed HTTPS origin')
    return value


async def renew_lease(client, job, *, interval=20):
    while True:
        await asyncio.sleep(interval)
        try:
            response = await client.post(f'/api/internal/jobs/{job.id}/renew', json={'lease_token': job.lease_token})
            response.raise_for_status()
        except (httpx.HTTPError, ValueError) as exc:
            # Any uncertainty about ownership stops work; the reconciler may reclaim
            # after lease expiry. Never post a stale success after renewal failure.
            raise LeaseLost('Job lease could not be renewed') from None


async def process_job(client, job: ClaimedJob, *, analyze_fn=analyze, renewal_interval=20):
    question = job.payload.question or 'Prepare an inspection brief for this confirmed resident report.'
    report = job.payload.report
    if job.kind == 'report_analysis' and report is None:
        outcome = JobResult(lease_token=job.lease_token, error='analysis_unavailable')
        response = await client.post(f'/api/internal/jobs/{job.id}/result', json=outcome.model_dump(mode='json'))
        response.raise_for_status()
        return 'invalid_payload'
    async def bounded_analysis():
        async with asyncio.timeout(90):
            return await analyze_fn(question, report)
    work = asyncio.create_task(bounded_analysis())
    renewer = asyncio.create_task(renew_lease(client, job, interval=renewal_interval))
    try:
        done, _ = await asyncio.wait([work, renewer], return_when=asyncio.FIRST_COMPLETED)
        if renewer in done:
            await renewer  # raises LeaseLost; do not write any outcome under uncertain ownership
        try:
            result = await work
            outcome = JobResult(lease_token=job.lease_token, result=result)
        except Exception as exc:
            # Provider errors can embed secrets or full prompts. Log type only.
            log.warning('analysis_failed job=%s error_type=%s', job.id, type(exc).__name__)
            outcome = JobResult(lease_token=job.lease_token, error='analysis_unavailable')
        # Lease renewal remains active until this idempotent write returns. The Site
        # independently checks token, lease expiry and immutable completed state.
        response = await client.post(f'/api/internal/jobs/{job.id}/result', json=outcome.model_dump(mode='json'))
        response.raise_for_status()
        return 'complete' if outcome.result else 'failed'
    finally:
        work.cancel()
        renewer.cancel()
        for task in [work, renewer]:
            with suppress(asyncio.CancelledError, LeaseLost, Exception):
                await task


async def drain_jobs(*, limit=3, client=None, analyze_fn=analyze):
    if not 1 <= limit <= 3: raise ValueError('Worker batch must be 1–3 jobs')
    if client is None:
        async with httpx.AsyncClient(base_url=site_origin(), headers={'Authorization': 'Bearer ' + os.environ['ROADLENS_INTERNAL_TOKEN']},
                                     timeout=15, follow_redirects=False, trust_env=False) as owned:
            return await drain_jobs(limit=limit, client=owned, analyze_fn=analyze_fn)
    completed = 0
    for _ in range(limit):
        response = await client.post('/api/internal/jobs/claim', json={})
        response.raise_for_status()
        claim = ClaimResponse.model_validate(response.json())
        if claim.job is None: break
        try:
            await process_job(client, claim.job, analyze_fn=analyze_fn)
            completed += 1
        except LeaseLost:
            log.warning('lease_lost job=%s', claim.job.id)
        except httpx.HTTPError as exc:
            # Lost result acknowledgement is safe: D1's receipt/lease rules decide
            # whether the minute reconciler can claim the job again.
            log.warning('job_callback_unconfirmed job=%s error_type=%s', claim.job.id, type(exc).__name__)
    return {'jobs_processed': completed}
