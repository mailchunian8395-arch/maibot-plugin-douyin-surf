from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import asyncio
import base64
import logging
import re
import shutil
import subprocess
import tempfile

logger = logging.getLogger(__name__)


class VideoDurationOutOfRangeError(ValueError):
    """视频时长超出自动观察上限，继续重试不会改变结果。"""


class VideoFileTooLargeError(ValueError):
    """视频文件超过 QQ 合并转发允许的体积。"""


def _write_browser_cookie_file(output_dir: Path, browser_cookies: list[dict[str, Any]] | None) -> Path | None:
    """把浏览器登录态转换为 yt-dlp 可读取的 Netscape Cookie 文件。"""

    if not browser_cookies:
        return None
    cookie_path = output_dir / "browser-cookies.txt"
    cookie_lines = ["# Netscape HTTP Cookie File", ""]
    for item in browser_cookies:
        domain = str(item.get("domain") or "").strip()
        name = str(item.get("name") or "").replace("\t", "").replace("\n", "")
        value = str(item.get("value") or "").replace("\t", "").replace("\n", "")
        if not domain or not name:
            continue
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        path = str(item.get("path") or "/")
        secure = "TRUE" if item.get("secure") else "FALSE"
        try:
            expires = max(0, int(float(item.get("expires") or 0)))
        except (TypeError, ValueError):
            expires = 0
        cookie_lines.append("\t".join((domain, include_subdomains, path, secure, str(expires), name, value)))
    cookie_path.write_text("\n".join(cookie_lines) + "\n", encoding="utf-8")
    return cookie_path


def _probe_video_duration(
    url: str,
    output_dir: Path,
    browser_cookies: list[dict[str, Any]] | None,
    browser_headers: dict[str, str] | None,
) -> int:
    """只读取视频元数据中的时长，绝不下载媒体文件。"""

    import yt_dlp

    options: dict[str, Any] = {
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        "logger": _YtDlpLogger(),
    }
    if browser_headers:
        options["http_headers"] = browser_headers
    if cookie_path := _write_browser_cookie_file(output_dir, browser_cookies):
        options["cookiefile"] = str(cookie_path)
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=False)
    return max(0, int(info.get("duration") or 0))


async def probe_video_duration(
    url: str,
    work_root: Path,
    *,
    browser_cookies: list[dict[str, Any]] | None = None,
    browser_headers: dict[str, str] | None = None,
) -> int:
    """在独立临时目录中读取时长，退出后不保留 Cookie 或媒体文件。"""

    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="video-probe-", dir=work_root) as temp:
        temp_dir = Path(temp).resolve()
        if work_root.resolve() not in temp_dir.parents:
            raise RuntimeError("视频时长探测临时目录越界")
        return await asyncio.to_thread(_probe_video_duration, url, temp_dir, browser_cookies, browser_headers)


def _download_image_bytes(
    url: str,
    max_bytes: int,
    browser_cookies: list[dict[str, Any]] | None,
) -> bytes:
    """下载登录态页面已经展示的单张图片，限制体积后交给 QQ 发送。"""

    cookie_header = "; ".join(
        f"{str(item.get('name') or '').strip()}={str(item.get('value') or '').strip()}"
        for item in browser_cookies or []
        if str(item.get("name") or "").strip()
    )
    headers = {
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": "https://www.douyin.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    request = Request(url, headers=headers)
    with urlopen(request, timeout=30) as response:
        image_bytes = response.read(max(1, int(max_bytes)) + 1)
    if not image_bytes:
        raise ValueError("图片响应为空")
    if len(image_bytes) > max(1, int(max_bytes)):
        raise VideoFileTooLargeError(
            f"图片文件 {len(image_bytes)} 字节超过单张分享上限 {max_bytes} 字节"
        )
    return image_bytes


async def download_images_for_share(
    image_urls: list[str],
    *,
    max_images: int,
    max_bytes_per_image: int,
    browser_cookies: list[dict[str, Any]] | None = None,
) -> list[str]:
    """下载抖音图文正文图片并编码成可直接发送到 QQ 的 Base64 数据。"""

    image_base64_list: list[str] = []
    for index, url in enumerate(image_urls[: max(1, int(max_images))], start=1):
        try:
            image_bytes = await asyncio.to_thread(
                _download_image_bytes,
                str(url),
                max_bytes_per_image,
                browser_cookies,
            )
        except Exception as exc:
            logger.warning("抖音图文图片下载失败 index=%s url=%s error=%s", index, url, exc)
            continue
        image_base64_list.append(base64.b64encode(image_bytes).decode("ascii"))
    return image_base64_list


class _YtDlpLogger:
    """把下载器输出接入插件日志，避免第三方警告直接污染 runner stderr。"""

    def debug(self, message: str) -> None:
        logger.debug("yt-dlp: %s", message)

    def info(self, message: str) -> None:
        logger.info("yt-dlp: %s", message)

    def warning(self, message: str) -> None:
        if "Unable to extract universal data for rehydration" in message:
            logger.debug("yt-dlp: %s", message)
            return
        logger.warning("yt-dlp: %s", message)

    def error(self, message: str) -> None:
        logger.error("yt-dlp: %s", message)


def _clean_vtt(text: str, max_chars: int) -> str:
    lines: list[str] = []
    last = ""
    for raw in text.splitlines():
        line = re.sub(r"<[^>]+>", "", raw).strip()
        if not line or line == "WEBVTT" or "-->" in line or line.isdigit():
            continue
        if line != last:
            lines.append(line)
            last = line
    return "\n".join(lines)[:max_chars]


def _download_video(
    url: str,
    output_dir: Path,
    max_duration: int,
    max_subtitle_chars: int,
    browser_cookies: list[dict[str, Any]] | None = None,
    browser_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    import yt_dlp

    template = str(output_dir / "video.%(ext)s")
    options = {
        "outtmpl": template,
        "format": "worstvideo[height<=720]+bestaudio/worst[height<=720]/best",
        "merge_output_format": "mp4",
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["zh-CN", "zh-Hans", "zh", "ja", "en"],
        "subtitlesformat": "vtt",
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        "logger": _YtDlpLogger(),
    }
    if browser_headers:
        options["http_headers"] = browser_headers
    if cookie_path := _write_browser_cookie_file(output_dir, browser_cookies):
        options["cookiefile"] = str(cookie_path)
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=False)
        duration = int(info.get("duration") or 0)
        if duration <= 0 or duration > max_duration:
            raise VideoDurationOutOfRangeError(f"视频时长 {duration}s 不在允许范围内")
        # 复用已经解析完成的元数据，避免为同一作品再次请求抖音页面，减少风控与浏览器指纹挑战。
        downloader.process_info(info)
        prepared = Path(downloader.prepare_filename(info))
    candidates = list(output_dir.glob("video.*"))
    video_path = next((item for item in candidates if item.suffix.lower() in {".mp4", ".webm", ".mkv"}), prepared)
    subtitle_path = next(iter(output_dir.glob("video*.vtt")), None)
    subtitle = _clean_vtt(subtitle_path.read_text(encoding="utf-8", errors="ignore"), max_subtitle_chars) if subtitle_path else ""
    return {
        "title": str(info.get("title") or ""),
        "description": str(info.get("description") or "")[:5000],
        "duration": duration,
        "uploader": str(info.get("uploader") or ""),
        "video_path": video_path,
        "subtitle": subtitle,
    }


def _sample_frames(video_path: Path, duration: int, count: int, output_dir: Path) -> list[bytes]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return []
    frames: list[bytes] = []
    for index in range(max(1, count)):
        timestamp = duration * (index + 0.5) / max(1, count)
        target = output_dir / f"frame-{index:02d}.jpg"
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{timestamp:.2f}", "-i", str(video_path), "-frames:v", "1", "-vf", "scale=640:-2", "-y", str(target)],
            check=True,
            timeout=90,
        )
        if target.is_file():
            frames.append(target.read_bytes())
    return frames


async def observe_video(
    url: str,
    work_root: Path,
    *,
    max_duration: int,
    frame_samples: int,
    max_subtitle_chars: int,
    browser_cookies: list[dict[str, Any]] | None = None,
    browser_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="video-", dir=work_root) as temp:
        temp_dir = Path(temp).resolve()
        if work_root.resolve() not in temp_dir.parents:
            raise RuntimeError("视频临时目录越界")
        result = await asyncio.to_thread(
            _download_video,
            url,
            temp_dir,
            max_duration,
            max_subtitle_chars,
            browser_cookies,
            browser_headers,
        )
        result["frames"] = await asyncio.to_thread(
            _sample_frames, result["video_path"], result["duration"], frame_samples, temp_dir
        )
        result.pop("video_path", None)
        return result


async def download_short_video_for_share(
    url: str,
    work_root: Path,
    *,
    max_duration: int,
    max_bytes: int,
    browser_cookies: list[dict[str, Any]] | None = None,
    browser_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """下载短视频并返回可直接编码为 QQ 视频段的 MP4 字节。

    发送前才下载，避免把大量临时视频长期落在插件目录；临时目录退出时会自动清理。
    """

    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="share-video-", dir=work_root) as temp:
        temp_dir = Path(temp).resolve()
        if work_root.resolve() not in temp_dir.parents:
            raise RuntimeError("分享视频临时目录越界")
        result = await asyncio.to_thread(
            _download_video,
            url,
            temp_dir,
            max_duration,
            0,
            browser_cookies,
            browser_headers,
        )
        video_path = result["video_path"]
        if not isinstance(video_path, Path) or not video_path.is_file():
            raise FileNotFoundError(f"下载器未生成可发送的视频文件: {video_path}")
        video_bytes = await asyncio.to_thread(video_path.read_bytes)
        if len(video_bytes) > max_bytes:
            raise VideoFileTooLargeError(
                f"视频文件 {len(video_bytes)} 字节超过分享上限 {max_bytes} 字节"
            )
        return {
            "title": str(result.get("title") or ""),
            "duration": int(result.get("duration") or 0),
            "video_base64": base64.b64encode(video_bytes).decode("ascii"),
        }
