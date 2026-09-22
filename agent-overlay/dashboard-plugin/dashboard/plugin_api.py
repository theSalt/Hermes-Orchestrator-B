"""hermes-attachments 后端：聊天附件上传 / 列表 / 内联预览 / Office→PDF 转换。

挂载：gateway 启动时把本模块的 ``router`` 以 /api/plugins/hermes-attachments/
挂进 dashboard 同一 FastAPI app（上游 web_server_dashboard._mount_plugin_api_routes），
会话 token 中间件自动覆盖本路由，无需自行鉴权。

安全边界：所有 path 参数经 ``_resolve_under_root`` 解析（realpath + is_relative_to），
只能命中 HERMES_HOME/attachments 子树，越界一律 404。阻塞操作（磁盘 IO /
soffice 子进程）全部走线程池，不堵事件循环。
"""

import asyncio
import hashlib
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

try:
    from hermes_constants import get_hermes_home
except ImportError:  # 本地自测 / 极端情况下的兜底（正常在镜像内必有）
    def get_hermes_home() -> str:
        return os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")


router = APIRouter()

ROOT = Path(get_hermes_home()) / "attachments"   # 即容器内 /opt/data/attachments
ROOT_RESOLVED = ROOT.resolve()                   # 统一用 resolve 后形态比较（防符号链接歧义）
PREVIEW_DIR = ROOT / ".preview"                  # PDF 转换缓存（/list 不展示）
MAX_UPLOAD_MB = int(os.environ.get("HERMES_PLUGIN_ATTACH_MAX_MB", "200"))
SOFFICE_TIMEOUT = int(os.environ.get("HERMES_PLUGIN_SOFFICE_TIMEOUT", "120"))
OFFICE_EXTS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
# mimetypes 注册表可能缺的 OOXML/OLE 类型，手工补齐
MIME_EXTRA = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
    ".md": "text/markdown",
    ".log": "text/plain",
}
_SOFFICE_LOCK = threading.Lock()  # 全局串行：2 核预算 + 杜绝 profile 锁竞争


def _ensure_dirs() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    PREVIEW_DIR.mkdir(exist_ok=True)


def _resolve_under_root(rel: str) -> Path:
    """把传入 path 解析并钉死在 ROOT 子树内；越界/非法一律 404（不区分不存在与越界）。

    接受两种形态：绝对路径（/upload 返回的就是绝对路径，前端原样回传）与
    相对 ROOT 的路径。越界校验对两者一视同仁。
    """
    raw = rel.strip()
    if not raw:
        raise HTTPException(status_code=404, detail="not found")
    p = Path(raw) if raw.startswith("/") else ROOT / raw
    p = p.resolve()
    if not p.is_relative_to(ROOT_RESOLVED) or p == ROOT_RESOLVED:
        raise HTTPException(status_code=404, detail="not found")
    return p


def _kind(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in OFFICE_EXTS:
        return "office"
    return "other"


def _guess_mime(p: Path) -> str:
    return MIME_EXTRA.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"


def _inline_disposition(name: str) -> str:
    """Content-Disposition: inline；CJK 用 filename*，ASCII 同步兜底。"""
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", name) or "file"
    return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


def _preview_key(p: Path) -> str:
    """转换缓存键：相对路径 + 大小 + mtime——原文件一变即换新键，天然失效。"""
    st = p.stat()
    digest = hashlib.sha256(
        f"{p.relative_to(ROOT_RESOLVED)}|{st.st_size}|{int(st.st_mtime)}".encode()
    ).hexdigest()[:24]
    return f"{digest}.pdf"


@router.post("/upload")
async def upload(file: UploadFile = File(...)):
    """multipart 单文件上传（前端多选即多次调用）；流式落盘，超限即断。"""
    _ensure_dirs()
    name = re.sub(r"[\x00-\x1f/\\]", "_", Path(file.filename or "file").name) or "file"
    subdir = ROOT / time.strftime("%Y-%m")
    cap = MAX_UPLOAD_MB * 1024 * 1024

    tmp = subdir / f".{uuid.uuid4().hex}.part"  # 同目录 part，os.replace 原子落位
    subdir.mkdir(parents=True, exist_ok=True)
    try:
        written = 0
        with open(tmp, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > cap:
                    raise HTTPException(
                        status_code=413, detail=f"超过大小上限 {MAX_UPLOAD_MB}MB"
                    )
                await run_in_threadpool(out.write, chunk)
        final, stem, ext, i = subdir / name, Path(name).stem, Path(name).suffix, 1
        while final.exists():  # 重名去重 -2、-3 …
            i += 1
            final = subdir / f"{stem}-{i}{ext}"
        await run_in_threadpool(os.replace, tmp, final)
    except HTTPException:
        tmp.unlink(missing_ok=True)
        raise
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    st = final.stat()
    return {
        "path": str(final),
        "name": final.name,
        "size": st.st_size,
        "mime": _guess_mime(final),
        "kind": _kind(final.name),
    }


@router.get("/list")
async def list_attachments():
    """attachments 子树清单（mtime 降序）；.preview 缓存与点文件不展示。"""

    def scan() -> list[dict]:
        _ensure_dirs()
        out = []
        for p in ROOT.rglob("*"):
            if not p.is_file() or PREVIEW_DIR in p.parents or p.name.startswith("."):
                continue
            st = p.stat()
            out.append(
                {
                    "path": str(p),
                    "name": p.name,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "mime": _guess_mime(p),
                    "kind": _kind(p.name),
                }
            )
        return sorted(out, key=lambda x: -x["mtime"])

    return {"root": str(ROOT), "items": await run_in_threadpool(scan)}


@router.get("/file")
async def get_file(path: str):
    """内联输出附件原件（正确 MIME + Content-Disposition: inline）。"""
    p = await run_in_threadpool(_resolve_under_root, path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(
        p,
        media_type=_guess_mime(p),
        headers={"Content-Disposition": _inline_disposition(p.name)},
    )


class ConvertReq(BaseModel):
    path: str


@router.post("/convert")
async def convert(req: ConvertReq):
    """Office → PDF，直接回 PDF 字节。缓存键含 mtime，原文件变更自动重转。"""
    p = await run_in_threadpool(_resolve_under_root, req.path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="not found")
    if p.suffix.lower() not in OFFICE_EXTS:
        raise HTTPException(status_code=400, detail="仅支持 Office 文档转换")
    cache = PREVIEW_DIR / await run_in_threadpool(_preview_key, p)
    if cache.is_file():
        return FileResponse(
            cache,
            media_type="application/pdf",
            headers={"Content-Disposition": _inline_disposition(p.stem + ".pdf")},
        )

    def run_soffice() -> None:
        with _SOFFICE_LOCK:
            with tempfile.TemporaryDirectory(prefix="ha-conv-") as td:
                # 每次转换独立 UserInstallation profile，避免并发 soffice 的 .lock 冲突
                profile = f"file:///tmp/ha-lo-{uuid.uuid4().hex}"
                r = subprocess.run(
                    [
                        "soffice", "--headless", f"-env:UserInstallation={profile}",
                        "--convert-to", "pdf", "--outdir", td, str(p),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=SOFFICE_TIMEOUT,
                )
                pdf = next(Path(td).glob("*.pdf"), None)
                if r.returncode != 0 or pdf is None:
                    tail = ((r.stderr or "") + (r.stdout or ""))[-300:]
                    raise RuntimeError(f"soffice 转换失败: {tail}")
                PREVIEW_DIR.mkdir(exist_ok=True)
                shutil.copy(pdf, cache)

    try:
        await asyncio.to_thread(run_soffice)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail=f"文档转换超时（>{SOFFICE_TIMEOUT}s）")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"文档转换失败: {e}")
    return FileResponse(
        cache,
        media_type="application/pdf",
        headers={"Content-Disposition": _inline_disposition(p.stem + ".pdf")},
    )


@router.delete("/file")
async def delete_file(path: str):
    """删除附件原件（best-effort 清理对应转换缓存）。"""
    p = await run_in_threadpool(_resolve_under_root, path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="not found")

    def _del() -> None:
        # 键含 mtime，须在 unlink 前算好
        try:
            stale = PREVIEW_DIR / _preview_key(p)
        except OSError:
            stale = None
        p.unlink(missing_ok=True)
        if stale is not None:
            stale.unlink(missing_ok=True)

    await run_in_threadpool(_del)
    return {"ok": True}
