"""Deploy with: modal deploy -m backend.modal_app

Set ROADLENS_CODE_COMMIT to the source commit when deploying. Secret roadlens-service
contains GEMINI_API_KEY, ROADLENS_MODAL_SERVICE_TOKEN, ROADLENS_INTERNAL_TOKEN and
ROADLENS_SITE_ORIGIN. No provider or deployment key is embedded in the image.
"""
from pathlib import Path
import os
import modal

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = 'roadlens-cambridge'
CLASSIFIER_MODEL = 'fastino/gliner2.5-base-v1'
CLASSIFIER_REVISION = '78cea040597df251eedefa9d7ee2a756af39fe64'
LABELS = ['obstructed visibility', 'pothole or damaged road surface', 'unsafe crossing',
          'blocked pavement or cycle lane', 'speeding concern', 'broken traffic signal or lighting',
          'other road concern', 'not a road report']

app = modal.App(APP_NAME)
service_secret = modal.Secret.from_name('roadlens-service')
weights = modal.Volume.from_name('roadlens-gliner-pinned-weights', create_if_missing=True)
agent_image = (modal.Image.debian_slim(python_version='3.12')
    .pip_install('pydantic-ai-slim[google]==2.46.0', 'pydantic-evals==2.46.0', 'pydantic==2.13.5',
                 'fastapi==0.141.1', 'httpx==0.28.1')
    .env({'ROADLENS_CODE_COMMIT': os.getenv('ROADLENS_CODE_COMMIT', 'working-tree-uncommitted'), 'PYDANTIC_AI_NO_BANNER': '1'})
    .add_local_dir(ROOT / 'backend', remote_path='/root/backend', ignore=['__pycache__', '*.pyc'])
    .add_local_file(ROOT / 'data' / 'roadlens_evidence.sqlite', remote_path='/root/data/roadlens_evidence.sqlite')
    .add_local_file(ROOT / 'data' / 'demo_places.json', remote_path='/root/data/demo_places.json'))
classifier_image = (modal.Image.debian_slim(python_version='3.12')
    .pip_install('torch==2.14.0+cpu', index_url='https://download.pytorch.org/whl/cpu')
    .pip_install('gliner2[local]==2.0.0', 'transformers==4.57.6', 'peft==0.21.0',
                 'huggingface-hub==0.36.2', 'pydantic==2.13.5')
    .env({'HF_HOME': '/weights/hf', 'TOKENIZERS_PARALLELISM': 'false', 'OMP_NUM_THREADS': '4'})
    .add_local_dir(ROOT / 'backend', remote_path='/root/backend', ignore=['__pycache__', '*.pyc']))


@app.cls(image=classifier_image, cpu=4, memory=8192, volumes={'/weights': weights},
         timeout=180, startup_timeout=600, scaledown_window=300, max_containers=2,
         min_containers=int(os.getenv('ROADLENS_CLASSIFIER_MIN_CONTAINERS', '0')))
@modal.concurrent(max_inputs=1)
class Classifier:
    @modal.enter()
    def load(self):
        import torch
        from huggingface_hub import snapshot_download
        from gliner2 import AutoExtractor
        torch.set_num_threads(4)
        local = snapshot_download(CLASSIFIER_MODEL, revision=CLASSIFIER_REVISION,
                                  cache_dir='/weights/hf', allow_patterns=['*.json', '*.safetensors', 'encoder_config/*'])
        weights.commit()
        self.extractor = AutoExtractor.from_pretrained(local, map_location='cpu')

    @modal.method()
    def predict(self, text: str):
        import time
        from backend.models import ClassifierSuggestion, EntitySpan
        if not isinstance(text, str) or not 1 <= len(text) <= 4000:
            raise ValueError('Classifier accepts a bounded resident transcript')
        started = time.perf_counter()
        prediction = self.extractor.classify_text(text, {'issue_type': LABELS})
        raw_entities = self.extractor.extract_entities(text,
            {'road': 'A named road, street, terrace, lane or junction in the resident statement'},
            include_confidence=True, include_spans=True)
        spans = []
        for item in raw_entities.get('entities', {}).get('road', [])[:50]:
            if not isinstance(item, dict): continue
            start, end = item.get('start'), item.get('end')
            if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(text): continue
            phrase = item.get('text', '')
            if text[start:end] != phrase: continue
            spans.append(EntitySpan(text=phrase, start=start, end=end, confidence=item.get('confidence')))
        # classify_text may return a label or a scored object; do not invent confidence.
        label = prediction.get('issue_type', '')
        confidence = None
        if isinstance(label, dict):
            confidence = label.get('confidence')
            label = label.get('label', '')
        if label not in LABELS: raise ValueError('Classifier returned an unknown label')
        return ClassifierSuggestion(model=CLASSIFIER_MODEL, revision=CLASSIFIER_REVISION,
            issue_label=label, entities=spans, confidence=confidence,
            runtime_seconds=time.perf_counter() - started).model_dump(mode='json')


@app.function(image=agent_image, secrets=[service_secret], cpu=1, memory=1024,
              timeout=350, max_containers=4)
async def worker():
    from backend.worker import drain_jobs
    return await drain_jobs(limit=3)


@app.function(image=agent_image, secrets=[service_secret], schedule=modal.Period(minutes=1),
              timeout=30, max_containers=1)
async def reconcile():
    # The same D1 claim protocol handles wake loss and expired leases. Scheduling
    # only a worker keeps the minute reconciler fast and avoids overlapping work.
    call = await worker.spawn.aio()
    return {'worker_call_id': call.object_id}


@app.function(image=agent_image, secrets=[service_secret], cpu=1, memory=1024,
              timeout=120, max_containers=3, scaledown_window=300)
@modal.concurrent(max_inputs=8)
@modal.asgi_app()
def api():
    from backend.service import create_api
    async def classify(text):
        return await Classifier().predict.remote.aio(text)
    async def wake():
        call = await worker.spawn.aio()
        return call.object_id
    return create_api(classify, wake)


@app.local_entrypoint()
async def warm():
    """Load pinned classifier weights and run a disclosed development smoke sentence."""
    result = await Classifier().predict.remote.aio(
        "I can't see past parked cars when I cross. Vicarage Terrace at St Matthews Street.")
    print({'model': result['model'], 'revision': result['revision'], 'issue_label': result['issue_label'],
           'runtime_seconds': result['runtime_seconds']})


@app.function(image=agent_image, timeout=30)
def runtime_manifest():
    """Export the exact resolved cloud environment for the deployment lock record."""
    import importlib.metadata
    import platform
    return {'python': platform.python_version(), 'model': 'gemini-3.8-flash',
            'code_commit': os.environ.get('ROADLENS_CODE_COMMIT', 'working-tree-uncommitted'),
            'packages': {p.metadata['Name']: p.version for p in importlib.metadata.distributions()}}

@app.function(image=agent_image, secrets=[service_secret], timeout=120)
async def development_smoke():
    """Unscored authored development fixture; private Modal RPC, no report creation."""
    from datetime import datetime, timezone
    from backend.agents import prepare, analyze
    from backend.models import PrepareRequest, ResidentTurn
    async def classify(text):
        return await Classifier().predict.remote.aio(text)
    try:
        result = await prepare(PrepareRequest(session_id='development-smoke', turns=[ResidentTurn(turn_id='development-turn', text='A paving slab is loose at York Street and New Street.', timestamp=datetime.now(timezone.utc))]), classifier=classify)
        evidence = await analyze('How many pedestrians were injured in Cambridge in 2025?')
        return {'intake':result.model_dump(mode='json'), 'evidence':evidence.model_dump(mode='json')}
    except Exception as e:
        message = str(e).replace(os.getenv('GEMINI_API_KEY','__absent__'), '[redacted]')
        return {'error_type':type(e).__name__, 'message':message[:1500]}
