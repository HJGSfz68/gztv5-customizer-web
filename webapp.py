from __future__ import annotations

import hmac
import os
import shutil
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).resolve().parent
RUNTIME = Path(os.environ.get('GZTV5_RUNTIME_DIR', ROOT / 'runtime')).resolve()
JOBS_DIR = RUNTIME / 'jobs'
BASE_APK = Path(os.environ.get('GZTV5_BASE_APK', ROOT / 'private' / 'base.apk')).resolve()
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
PUBLIC_MODE = os.environ.get('PUBLIC_MODE', '0').lower() in ('1', 'true', 'yes')
SESSION_SECRET = os.environ.get('APP_SECRET_KEY', '')
COOKIE_SECURE = os.environ.get('COOKIE_SECURE', '1').lower() not in ('0', 'false', 'no')
MAX_ICON_BYTES = 20 * 1024 * 1024
JOB_TTL_SECONDS = 24 * 60 * 60
MAX_ACTIVE_JOBS = 1 if PUBLIC_MODE else 8

sys.path.insert(0, str(ROOT))
import customize_gztv5 as customizer  # noqa: E402

customizer.DEFAULT_BASE = BASE_APK

app = FastAPI(title='GZTV5 APK 定制台', docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET or 'development-only-secret-change-me',
    max_age=60 * 60,
    same_site='strict',
    https_only=COOKIE_SECURE,
)

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
LOGIN_FAILURES: dict[str, tuple[int, float]] = {}
LOGIN_LOCK = threading.Lock()
EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix='apk-build')


class LoginPayload(BaseModel):
    password: str


def require_admin(request: Request) -> None:
    if PUBLIC_MODE:
        return
    if request.session.get('admin') is not True:
        raise HTTPException(status_code=401, detail='请先登录。')


def check_runtime_config() -> None:
    if not PUBLIC_MODE and (len(ADMIN_PASSWORD) < 16 or ADMIN_PASSWORD == 'CHANGE_ME'):
        raise RuntimeError('请在 .env 中设置至少 16 位的 ADMIN_PASSWORD。')
    if len(SESSION_SECRET) < 32 or SESSION_SECRET == 'CHANGE_ME':
        raise RuntimeError('请在 .env 中设置至少 32 位的 APP_SECRET_KEY。')
    keystore = customizer.KEYSTORE
    passfile = customizer.PASSFILE
    missing = [str(path) for path in (BASE_APK, keystore, passfile) if not path.is_file()]
    if missing:
        raise RuntimeError('服务器缺少基准 APK 或签名文件：' + ', '.join(missing))
    for tool in ('apktool', 'aapt', 'apksigner', 'zipalign', 'smali'):
        if shutil.which(tool) is None:
            raise RuntimeError(f'服务器缺少构建工具：{tool}')
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    # Jobs are deliberately ephemeral; remove leftovers after an unclean restart.
    for child in JOBS_DIR.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


@app.on_event('startup')
def startup() -> None:
    check_runtime_config()


@app.get('/', response_class=HTMLResponse)
def index() -> str:
    html = (ROOT / 'index.html').read_text(encoding='utf-8')
    mode = '1' if PUBLIC_MODE else '0'
    return html.replace('<body>', f'<body data-public-mode="{mode}">', 1)


@app.get('/healthz')
def healthz() -> dict[str, str]:
    return {'status': 'ok'}


@app.post('/api/login')
def login(payload: LoginPayload, request: Request) -> JSONResponse:
    if PUBLIC_MODE:
        raise HTTPException(status_code=404, detail='公开模式已关闭管理员登录。')
    ip = request.client.host if request.client else 'unknown'
    now = time.time()
    with LOGIN_LOCK:
        failures, blocked_until = LOGIN_FAILURES.get(ip, (0, 0.0))
        if now < blocked_until:
            raise HTTPException(status_code=429, detail='尝试次数过多，请 15 分钟后再试。')
    if not hmac.compare_digest(payload.password, ADMIN_PASSWORD):
        with LOGIN_LOCK:
            failures, blocked_until = LOGIN_FAILURES.get(ip, (0, 0.0))
            failures += 1
            LOGIN_FAILURES[ip] = (failures, now + 15 * 60 if failures >= 8 else 0.0)
        raise HTTPException(status_code=401, detail='密码不正确。')
    with LOGIN_LOCK:
        LOGIN_FAILURES.pop(ip, None)
    request.session.clear()
    request.session['admin'] = True
    return JSONResponse({'ok': True})


@app.post('/api/logout', dependencies=[Depends(require_admin)])
def logout(request: Request) -> dict[str, bool]:
    if PUBLIC_MODE:
        raise HTTPException(status_code=404, detail='公开模式不使用管理员会话。')
    request.session.clear()
    return {'ok': True}


@app.get('/api/status', dependencies=[Depends(require_admin)])
def status() -> dict[str, Any]:
    with JOBS_LOCK:
        active = sum(job['status'] in ('queued', 'running') for job in JOBS.values())
    return {
        'ready': BASE_APK.is_file() and customizer.KEYSTORE.is_file() and customizer.PASSFILE.is_file(),
        'active_jobs': active,
        'base_apk': BASE_APK.name,
        'session_minutes': 60,
        'public_mode': PUBLIC_MODE,
    }


def prune_old_jobs() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    with JOBS_LOCK:
        expired = [job_id for job_id, job in JOBS.items()
                   if job['status'] not in ('queued', 'running') and job['created_at'] < cutoff]
        for job_id in expired:
            JOBS.pop(job_id, None)
            shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)


def update_job(job_id: str, **values: Any) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(values)


def run_build(job_id: str, display_name: str, package_name: str, web_url: str,
              icon_path: Path | None, output_path: Path) -> None:
    update_job(job_id, status='running', progress='准备构建…')

    def report(message: str) -> None:
        update_job(job_id, progress=message)

    try:
        customizer.customize(
            base_apk=BASE_APK,
            output_apk=output_path,
            display_name=display_name,
            package_name=package_name,
            web_url=web_url,
            icon_path=icon_path,
            progress_callback=report,
            keep_failed_work=False,
        )
        update_job(job_id, status='completed', progress='构建与签名校验完成',
                   output_name=f'{package_name}.apk', finished_at=time.time())
    except Exception as exc:
        output_path.unlink(missing_ok=True)
        update_job(job_id, status='failed', progress='构建失败', error=str(exc)[:1600],
                   finished_at=time.time())
    finally:
        if icon_path is not None:
            icon_path.unlink(missing_ok=True)


@app.post('/api/build', dependencies=[Depends(require_admin)])
async def build(
    display_name: str = Form(...),
    package_name: str = Form(...),
    web_url: str = Form(...),
    icon: UploadFile | None = File(default=None),
) -> dict[str, str]:
    try:
        customizer.validate_inputs(display_name, package_name, web_url)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    icon_bytes: bytes | None = None
    if icon is not None and icon.filename:
        icon_bytes = await icon.read(MAX_ICON_BYTES + 1)
        if len(icon_bytes) > MAX_ICON_BYTES:
            raise HTTPException(status_code=413, detail='图标文件不能超过 20 MB。')
        if icon_bytes:
            try:
                from PIL import Image
                import io
                with Image.open(io.BytesIO(icon_bytes)) as image:
                    if image.width * image.height > 25_000_000:
                        raise ValueError('图标分辨率超过服务器安全上限。')
                    image.verify()
            except Exception as exc:
                raise HTTPException(status_code=422, detail='图标无法识别，请上传 PNG、JPG 或 WebP 图片。') from exc

    prune_old_jobs()
    with JOBS_LOCK:
        active = sum(job['status'] in ('queued', 'running') for job in JOBS.values())
        if active >= MAX_ACTIVE_JOBS:
            raise HTTPException(status_code=429, detail='当前构建队列已满，请稍后再试。')
        job_id = uuid.uuid4().hex
        job_dir = JOBS_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        icon_path: Path | None = None
        if icon_bytes:
            icon_path = job_dir / 'uploaded-icon'
            icon_path.write_bytes(icon_bytes)
        output_path = job_dir / f'{package_name}.apk'
        JOBS[job_id] = {
            'status': 'queued', 'progress': '排队中', 'created_at': time.time(),
            'output_path': str(output_path), 'output_name': f'{package_name}.apk',
        }
        EXECUTOR.submit(run_build, job_id, display_name, package_name, web_url,
                        icon_path, output_path)
    return {'job_id': job_id}


@app.get('/api/jobs/{job_id}', dependencies=[Depends(require_admin)])
def get_job(job_id: str) -> dict[str, Any]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail='任务不存在或已过期。')
        result = {key: value for key, value in job.items()
                  if key not in ('output_path', 'created_at', 'finished_at')}
    result['download_url'] = f'/api/jobs/{job_id}/download' if result['status'] == 'completed' else None
    return result


@app.get('/api/jobs/{job_id}/download', dependencies=[Depends(require_admin)])
def download(job_id: str) -> FileResponse:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None or job['status'] != 'completed':
            raise HTTPException(status_code=404, detail='APK 尚未生成或已过期。')
        path = Path(job['output_path'])
        filename = job['output_name']
    if not path.is_file():
        raise HTTPException(status_code=404, detail='APK 文件不存在或已清理。')
    return FileResponse(path, media_type='application/vnd.android.package-archive',
                        filename=filename, headers={'Cache-Control': 'no-store'})
