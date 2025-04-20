import os
import asyncio
import json
from typing import List, Optional, Dict, Any
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, Form, BackgroundTasks, Query
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import aiofiles
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="YouTube Downloader")

# 创建下载目录
BASE_DIR = Path(__file__).resolve().parent
# 修改下载目录设置，使用相对路径
DOWNLOAD_DIR = Path("downloads")
if not DOWNLOAD_DIR.exists():
    DOWNLOAD_DIR.mkdir(parents=True)
DOWNLOAD_DIR.mkdir(exist_ok=True)

# 创建静态文件目录
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)

# 挂载静态文件
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/downloads", StaticFiles(directory=str(DOWNLOAD_DIR)), name="downloads")

# 设置模板
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# 存储下载进度的全局变量
download_progress = {}

# 存储已下载视频信息的文件
VIDEOS_INFO_FILE = BASE_DIR / "videos_info.json"

# 视频信息模型
class VideoInfo(BaseModel):
    id: str
    title: str
    duration: int
    author: str
    description: str
    file_size: str
    file_path: str
    thumbnail: str
    download_date: str

# 加载已下载视频信息
def load_videos_info() -> List[VideoInfo]:
    if not VIDEOS_INFO_FILE.exists():
        return []
    try:
        with open(VIDEOS_INFO_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return [VideoInfo(**item) for item in data]
    except Exception as e:
        print(f"Error loading videos info: {e}")
        return []

# 保存视频信息
async def save_video_info(video_info: VideoInfo):
    videos = load_videos_info()
    # 检查是否已存在相同ID的视频
    videos = [v for v in videos if v.id != video_info.id]
    videos.append(video_info)
    
    async with aiofiles.open(VIDEOS_INFO_FILE, "w", encoding="utf-8") as f:
        await f.write(json.dumps([v.model_dump() for v in videos], ensure_ascii=False, indent=2))

# 自定义进度钩子
def progress_hook(d):
    video_id = d.get('info_dict', {}).get('id', 'unknown')
    
    if d['status'] == 'downloading':
        downloaded = d.get('downloaded_bytes', 0)
        total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
        
        if total > 0:
            progress = (downloaded / total) * 100
            download_progress[video_id] = {
                'progress': round(progress, 2),
                'speed': d.get('speed', 0),
                'eta': d.get('eta', 0),
                'status': 'downloading'
            }
    
    elif d['status'] == 'finished':
        download_progress[video_id] = {
            'progress': 100,
            'status': 'finished'
        }

# 获取视频可用格式
async def get_video_formats(url: str):
    ydl_opts = {
        'skip_download': True,
        'quiet': True,
        'no_warnings': True,
    }
    
    loop = asyncio.get_event_loop()
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
            
            # 提取视频格式信息
            formats = []
            for f in info.get('formats', []):
                # 只选择有视频流的格式
                if f.get('vcodec') != 'none' and f.get('resolution') != 'audio only':
                    format_info = {
                        'format_id': f.get('format_id'),
                        'resolution': f.get('resolution', 'unknown'),
                        'ext': f.get('ext', 'mp4'),
                        'fps': f.get('fps', 0),
                        'filesize': f.get('filesize', 0),
                        'format_note': f.get('format_note', ''),
                        'vcodec': f.get('vcodec', ''),
                    }
                    formats.append(format_info)
            
            # 按分辨率排序（从高到低）
            formats.sort(key=lambda x: (
                0 if x['resolution'] == 'unknown' else 
                int(x['resolution'].split('x')[1]) if 'x' in x['resolution'] else 
                int(x['resolution'].rstrip('p'))
            ), reverse=True)
            
            return {
                'id': info.get('id', 'unknown'),
                'title': info.get('title', 'Unknown'),
                'formats': formats
            }
    except Exception as e:
        print(f"Error getting video formats: {e}")
        return {'error': str(e)}

# 异步下载视频
async def download_video(url: str, format_id: str = None):
    # 基本下载配置
    ydl_opts = {
        'format': format_id if format_id else 'best',
        'outtmpl': str(DOWNLOAD_DIR / '%(title)s-%(id)s.%(ext)s'),
        'progress_hooks': [progress_hook],
        'quiet': False,
        'no_warnings': False,
        'socket_timeout': 60,  # 延长超时时间到60秒
        'retries': 10,  # 增加重试次数到10次
        'retry_sleep': lambda n: 5 * (n + 1),  # 重试间隔递增
        'ignoreerrors': False,  # 不忽略错误，以便我们可以捕获它们
        'source_address': '0.0.0.0',  # 允许所有网络接口
    }
    
    # 添加代理支持
    proxy = os.environ.get('HTTP_PROXY') or os.environ.get('HTTPS_PROXY')
    if proxy:
        ydl_opts.update({
            'proxy': proxy,
            'socket_timeout': 120,  # 使用代理时延长超时时间
        })
    
    loop = asyncio.get_event_loop()
    
    # 先获取视频信息
    info_opts = dict(ydl_opts)
    info_opts['skip_download'] = True
    
    try:
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
            video_id = info.get('id', 'unknown')
            
            # 设置初始进度
            download_progress[video_id] = {
                'progress': 0,
                'status': 'starting'
            }
            
            # 下载视频
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=True))
            
            # 获取下载后的文件信息
            filename = f"{info['title']}-{info['id']}.{info['ext']}"
            file_path = DOWNLOAD_DIR / filename
            file_size = os.path.getsize(file_path) / (1024 * 1024)  # MB
            
            # 保存视频信息
            video_info = VideoInfo(
                id=info['id'],
                title=info['title'],
                duration=info.get('duration', 0),
                author=info.get('uploader', 'Unknown'),
                description=info.get('description', '')[:500],  # 限制描述长度
                file_size=f"{file_size:.2f} MB",
                file_path=f"/downloads/{filename}",
                thumbnail=info.get('thumbnail', ''),
                download_date=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
            
            await save_video_info(video_info)
            return video_info
            
    except Exception as e:
        video_id = url.split("v=")[-1].split("&")[0] if "v=" in url else "unknown"
        error_message = str(e)
        
        # 提供更友好的错误消息
        if "HTTP Error 429" in error_message:
            error_message = "YouTube 限制了请求速率，请稍后再试。"
        elif "HTTP Error 403" in error_message:
            error_message = "无法访问此视频，可能是地区限制或需要登录。"
        elif "HTTP Error 404" in error_message:
            error_message = "视频不存在或已被删除。"
        elif "Unable to download API page" in error_message or "timed out" in error_message.lower() or "WinError 10060" in error_message:
            error_message = "网络连接超时，建议：\n1. 检查网络连接\n2. 尝试使用代理服务器\n3. 稍后重试"
        elif "This video is unavailable" in error_message:
            error_message = "此视频不可用，可能已被设为私有或删除。"
        elif "Video unavailable" in error_message:
            error_message = "视频不可用，可能已被上传者删除或设为私有。"
        elif "Sign in" in error_message:
            error_message = "此视频需要登录才能观看。"
        elif "The uploader has not made this video available" in error_message:
            error_message = "上传者未在您的国家/地区提供此视频。"
        elif "socket" in error_message.lower() or "network" in error_message.lower():
            error_message = "网络连接不稳定，建议：\n1. 检查网络连接\n2. 尝试使用代理服务器\n3. 稍后重试"
        elif "DNS" in error_message:
            error_message = "DNS解析失败，建议：\n1. 检查网络DNS设置\n2. 尝试使用其他DNS服务器\n3. 使用代理服务器"
        
        download_progress[video_id] = {
            'progress': 0,
            'status': 'error',
            'error': error_message,
            'original_error': str(e)  # 保存原始错误信息以便调试
        }
        print(f"Error downloading video: {e}")
        return None

# 路由
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    videos = load_videos_info()
    return templates.TemplateResponse(
        "index.html", 
        {"request": request, "videos": videos}
    )

@app.get("/video-formats/")
async def get_formats(url: str = Query(...)):
    # 验证URL格式
    if not url or not any(domain in url for domain in ['youtube.com', 'youtu.be']):
        return {"status": "error", "message": "请输入有效的YouTube视频链接"}
    
    # 获取视频格式
    formats_info = await get_video_formats(url)
    
    if 'error' in formats_info:
        return {"status": "error", "message": formats_info['error']}
    
    return {"status": "success", "data": formats_info}

@app.post("/download/")
async def download(background_tasks: BackgroundTasks, url: str = Form(...), format_id: Optional[str] = Form(None)):
    # 验证URL格式
    if not url or not any(domain in url for domain in ['youtube.com', 'youtu.be']):
        return {"status": "error", "message": "请输入有效的YouTube视频链接"}
    
    # 从URL中提取视频ID
    if "youtube.com" in url and "v=" in url:
        video_id = url.split("v=")[-1].split("&")[0]
    elif "youtu.be" in url:
        video_id = url.split("/")[-1].split("?")[0]
    else:
        video_id = "unknown"
        
    if video_id == "unknown":
        return {"status": "error", "message": "无法识别视频ID，请检查链接格式"}
    
    # 设置初始进度
    download_progress[video_id] = {
        'progress': 0,
        'status': 'queued'
    }
    
    # 在后台任务中下载视频
    background_tasks.add_task(download_video, url, format_id)
    
    return {"status": "success", "message": "Download started", "video_id": video_id}

@app.get("/progress/{video_id}")
async def get_progress(video_id: str):
    if video_id in download_progress:
        return download_progress[video_id]
    return {"status": "not_found"}

@app.get("/videos/")
async def get_videos():
    videos = load_videos_info()
    return videos

# 本地开发时使用，部署时会被忽略
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)