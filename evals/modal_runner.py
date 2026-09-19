"""Optional cloud evaluation execution, for hosts without direct Gemini connectivity.

From the integrated project: modal run -m evals.modal_runner
This is an explicit paid evaluation invocation; importing the module runs no cases.
"""
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone
import modal

LOCAL = modal.is_local()
ROOT = Path(__file__).resolve().parents[1] if LOCAL else Path('/root/roadlens')
# Remote hydration has neither git nor the host checkout. Provenance was fixed
# while building the image and reaches containers through explicit environment.
COMMIT = os.getenv('ROADLENS_CODE_COMMIT', 'uncommitted')
DIRTY = os.getenv('ROADLENS_SOURCE_DIRTY', 'true')
if LOCAL:
    try:
        COMMIT = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], stderr=subprocess.DEVNULL, text=True).strip()
        DIRTY = str(bool(subprocess.check_output(['git', '-C', str(ROOT), 'status', '--porcelain'], text=True).strip())).lower()
    except (OSError, subprocess.CalledProcessError):
        pass
app = modal.App('roadlens-authored-evaluation')
image = (modal.Image.debian_slim(python_version='3.12')
    .pip_install('pydantic-ai-slim[google]==2.46.0', 'pydantic-evals==2.46.0', 'pydantic==2.13.5', 'httpx==0.28.1', 'modal==1.5.5')
    .env({'ROADLENS_CODE_COMMIT': COMMIT, 'ROADLENS_SOURCE_DIRTY': DIRTY, 'PYDANTIC_AI_NO_BANNER': '1', 'PYTHONPATH': '/root/roadlens'}))
if LOCAL:
    # Host mounts are configured once when constructing the deployment. Do not
    # read or remount host files while Modal imports this module in a container.
    image = (image
        .add_local_dir(ROOT / 'backend', '/root/roadlens/backend', ignore=['__pycache__', '*.pyc'])
        .add_local_dir(ROOT / 'evals', '/root/roadlens/evals', ignore=['__pycache__', '*.pyc'])
        .add_local_dir(ROOT / 'box_adapter', '/root/roadlens/box_adapter', ignore=['__pycache__', '*.pyc'])
        .add_local_file(ROOT / 'research/frozen-inputs.json', '/root/roadlens/research/frozen-inputs.json'))
    # Copy exactly frozen data inputs; no complete county workbook is published.
    for relative in json.loads((ROOT / 'research/frozen-inputs.json').read_text())['sha256']:
        if relative.startswith('data/'):
            image = image.add_local_file(ROOT / relative, '/root/roadlens/' + relative)


@app.function(image=image, secrets=[modal.Secret.from_name('roadlens-service')], cpu=1, memory=2048, timeout=14400)
async def evaluate_cloud(repetitions: int = 1):
    from argparse import Namespace
    from evals.run_suite import execute
    import tempfile
    output = Path(tempfile.mkdtemp()) / 'results'
    args = Namespace(project_root='/root/roadlens', output=str(output), arms=['A', 'B', 'C'], repetitions=repetitions,
                     modal_app='roadlens-cambridge', validate_only=False, execute=True)
    await execute(args)
    return {p.name: p.read_text() for p in output.iterdir() if p.is_file()}


@app.function(image=image, secrets=[modal.Secret.from_name('roadlens-service')], cpu=1, memory=1024, timeout=120)
async def smoke_baseline():
    from datetime import datetime, timezone
    from backend.evidence import Repository
    from backend.models import PrepareRequest, ResidentTurn
    from evals.baseline import run
    request = PrepareRequest(session_id='development-smoke', turns=[ResidentTurn(turn_id='development-turn',
        text='A paving slab is loose at York Street and New Street.', timestamp=datetime.now(timezone.utc))])
    result, calls = await run('intake', repo=Repository(Path('/root/roadlens/data')), request=request)
    return {'scope': 'Unscored development smoke; not one of the frozen 40 scenarios', 'result': result, 'raw_model_calls': calls}


@app.local_entrypoint()
async def main(repetitions: int = 1, smoke: bool = False):
    if smoke:
        result = await smoke_baseline.remote.aio()
        print(json.dumps(result, indent=2))
        return
    if repetitions not in (1, 3):
        raise ValueError('Use one run initially, or three explicitly budgeted repetitions')
    artifacts = await evaluate_cloud.remote.aio(repetitions)
    output = ROOT / 'research' / ('evaluation-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, text in artifacts.items():
        if name not in ('manifest.json', 'cases.jsonl', 'summary.json', 'pydantic-evals-report.json'):
            raise ValueError('Unexpected result artifact')
        (output / name).write_text(text)
    print('Evaluation artifacts saved to', output)
