"""
Camera Watchdog - Camera Connection Checks (v2)
Handles ping, RTSP connection, and frame capture.
v2: multi-vendor — rtsps (TLS), ONVIF profile discovery, RTSP path fallback probing.
"""
import logging
import socket
import subprocess
import time
from dataclasses import dataclass
from enum import Enum

import cv2
import numpy as np

from modules.config_loader import CameraConfig, build_rtsp_url

logger = logging.getLogger(__name__)


class CheckResult(Enum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    CONNECTION_REFUSED = "connection_refused"
    AUTH_FAILED = "auth_failed"
    NOT_FOUND = "not_found"
    ERROR = "error"


@dataclass
class PingResult:
    success: bool
    latency_ms: float
    error: str = ""


@dataclass
class RTSPResult:
    success: bool
    result_type: CheckResult
    frame: np.ndarray | None = None
    error: str = ""
    latency_ms: float = 0.0
    resolved_path: str = ""   # v2: шлях що запрацював (fallback probing)


def ping_camera(ip: str, timeout: float = 2.0) -> PingResult:
    """Ping camera IP address."""
    start = time.time()
    try:
        # Try ICMP ping first
        result = subprocess.run(
            ['ping', '-c', '1', '-W', str(int(timeout)), ip],
            capture_output=True,
            timeout=timeout + 1
        )
        if result.returncode == 0:
            output = result.stdout.decode()
            for line in output.split('\n'):
                if 'time=' in line:
                    latency_str = line.split('time=')[1].split(' ')[0]
                    try:
                        latency = float(latency_str)
                        return PingResult(True, latency)
                    except ValueError:
                        pass
            return PingResult(True, (time.time() - start) * 1000)
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass

    # TCP ping fallback (port 80 or 554)
    for port in [80, 554, 443]:
        try:
            sock_start = time.time()
            sock = socket.create_connection((ip, port), timeout=timeout)
            sock.close()
            return PingResult(True, (time.time() - sock_start) * 1000)
        except TimeoutError:
            return PingResult(False, 0, "timeout")
        except ConnectionRefusedError:
            return PingResult(False, 0, "connection_refused")
        except OSError as e:
            return PingResult(False, 0, str(e))

    return PingResult(False, 0, "all_ports_failed")


def _ffprobe(rtsp_url: str, camera: CameraConfig) -> tuple[int, str, str]:
    """Run ffprobe against rtsp url; return (returncode, stdout, stderr)."""
    cmd = [
        'ffprobe',
        '-rtsp_transport', camera.rtsp_transport,
        '-i', rtsp_url,
        '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height,codec_name,r_frame_rate',
        '-of', 'csv=p=0'
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=camera.rtsp_timeout)
    return result.returncode, result.stdout.decode('utf-8', 'ignore'), result.stderr.decode('utf-8', 'ignore')


def _classify_ffprobe_error(stderr: str) -> CheckResult:
    if '401' in stderr or 'Unauthorized' in stderr:
        return CheckResult.AUTH_FAILED
    if '404' in stderr or 'Not Found' in stderr:
        return CheckResult.NOT_FOUND
    if 'timeout' in stderr.lower():
        return CheckResult.TIMEOUT
    if 'Connection refused' in stderr:
        return CheckResult.CONNECTION_REFUSED
    return CheckResult.ERROR


def check_rtsp_connection(camera: CameraConfig, username: str | None = None,
                          password: str | None = None) -> RTSPResult:
    """
    Check RTSP connection; on 404 probes vendor fallback paths (v2).
    v2.2: ffprobe + capture об'єднані — на камеру відкривається ОДНА RTSP-сесія
    за перевірку (Hikvision вичерпує сесійний пул при 2 сесіях/check × частоті).
    """
    start = time.time()

    candidate_paths: list[str] = [camera.rtsp_path]
    for p in (camera.fallback_paths or []):
        if p not in candidate_paths:
            candidate_paths.append(p)

    for idx, path in enumerate(candidate_paths):
        url = build_rtsp_url(camera, username, password, path=path)
        try:
            code, _out, err = _ffprobe(url, camera)
        except subprocess.TimeoutExpired:
            return RTSPResult(False, CheckResult.TIMEOUT, error="ffprobe timeout",
                              latency_ms=(time.time() - start) * 1000)
        except Exception as e:
            return RTSPResult(False, CheckResult.ERROR, error=str(e),
                              latency_ms=(time.time() - start) * 1000)

        if code != 0:
            kind = _classify_ffprobe_error(err)
            if kind is CheckResult.NOT_FOUND and idx < len(candidate_paths) - 1:
                logger.info(f"{camera.id}: path {path!r} → not found, probing fallback ({idx + 2}/{len(candidate_paths)})")
                continue
            return RTSPResult(False, kind, error=err[:200],
                              latency_ms=(time.time() - start) * 1000,
                              resolved_path=path)

        # Успіх: якщо шлях відрізняється від конфігу — запам'ятовуємо в об'єкті камери
        if path != camera.rtsp_path:
            logger.info(f"{camera.id}: fallback path accepted → {path!r} (used from now on)")
            camera.rtsp_path = path
        frame = capture_frame(camera, username, password)
        latency_ms = (time.time() - start) * 1000
        if frame is not None:
            return RTSPResult(True, CheckResult.SUCCESS, frame=frame,
                              latency_ms=latency_ms, resolved_path=path)
        return RTSPResult(False, CheckResult.ERROR, error="Failed to capture frame",
                          latency_ms=latency_ms, resolved_path=path)

    return RTSPResult(False, CheckResult.NOT_FOUND, error="All RTSP paths exhausted",
                      latency_ms=(time.time() - start) * 1000)


def capture_frame(camera: CameraConfig, username: str | None = None,
                  password: str | None = None, path: str | None = None) -> np.ndarray | None:
    """
    Capture a single frame from RTSP stream using ffmpeg.
    v2.2: -timeout (мкс) — ffmpeg САМ закриває RTSP TEARDOWN-ом при помилці/обриві,
    замість kill → камера не копить мертві сесії (Operation not permitted).
    -rw_timeout guards мережеві зависання на read-стадії.
    """
    rtsp_url = build_rtsp_url(camera, username, password, path=path)

    try:
        cmd = [
            'ffmpeg',
            '-rtsp_transport', camera.rtsp_transport,
            '-i', rtsp_url,
            # v2.4: timeout — protocol-private option RTSP demuxer, МУСКАЄ бути ПІСЛЯ -i.
            # 5с socket I/O timeout (мкс) → graceful RTSP teardown при обриві (не kill),
            # камери Hikvision не коплять мертві сесії ("Operation not permitted")
            '-timeout', '5000000',
            '-vframes', '1',
            '-f', 'image2pipe',
            '-vcodec', 'mjpeg',
            '-q:v', '2',
            '-'
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=camera.rtsp_timeout + 5)
        if result.returncode == 0 and result.stdout:
            nparr = np.frombuffer(result.stdout, np.uint8)
            frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            return frame
    except subprocess.TimeoutExpired:
        logger.warning(f"Frame capture timeout for {camera.id}")
    except Exception as e:
        logger.warning(f"Frame capture error for {camera.id}: {e}")

    return None


def discover_onvif_profiles(camera: CameraConfig, username: str | None = None,
                            password: str | None = None) -> list[str]:
    """
    ONVIF GetProfiles via raw SOAP over HTTP (без python-onvif залежності).
    Повертає список rtsp:// URI, які камера рекламує (v2 informational helper).
    """
    soap_envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        '<s:Body xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<GetProfiles xmlns="http://www.onvif.org/ver10/media/wsdl"/>'
        '</s:Body></s:Envelope>'
    )
    results: list[str] = []

    import urllib.request
    import base64
    http_port = 80 if camera.port == 554 else camera.port
    for onvif_path in ("/onvif/device_service", "/onvif/media_service"):
        try:
            url = f"http://{camera.ip}:{http_port}{onvif_path}"
            req = urllib.request.Request(
                url, data=soap_envelope.encode(),
                headers={
                    "Content-Type": "application/soap+xml; charset=utf-8",
                    "Authorization": "Basic " + base64.b64encode(
                        f"{username or 'admin'}:{password or ''}".encode()).decode(),
                }, method="POST"
            )
            with urllib.request.urlopen(req, timeout=max(5, camera.rtsp_timeout)) as resp:
                body = resp.read().decode('utf-8', 'ignore')
            import re
            for match in re.findall(r'(rtsp[s]?://[^<"\']+?)(?:<|")', body):
                results.append(match.strip())
        except Exception as e:
            logger.debug(f"ONVIF probe {camera.id} {onvif_path}: {e}")

    seen: set[str] = set()
    unique = []
    for uri in results:
        if uri not in seen:
            seen.add(uri)
            unique.append(uri)
    return unique


def get_stream_info(camera: CameraConfig, username: str | None = None,
                    password: str | None = None) -> dict | None:
    """Get stream information (resolution, codec, fps)."""
    rtsp_url = build_rtsp_url(camera, username, password)

    try:
        cmd = [
            'ffprobe',
            '-rtsp_transport', camera.rtsp_transport,
            '-i', rtsp_url,
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height,codec_name,r_frame_rate,bit_rate',
            '-of', 'json'
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=camera.rtsp_timeout)
        if result.returncode == 0:
            import json
            data = json.loads(result.stdout.decode())
            if data.get('streams'):
                stream = data['streams'][0]
                fps_str = stream.get('r_frame_rate', '0/1')
                if '/' in fps_str:
                    num, den = fps_str.split('/')
                    fps = float(num) / float(den) if float(den) > 0 else 0
                else:
                    fps = float(fps_str)

                return {
                    'width': stream.get('width'),
                    'height': stream.get('height'),
                    'codec': stream.get('codec_name'),
                    'fps': fps,
                    'bitrate': stream.get('bit_rate')
                }
    except Exception as e:
        logger.warning(f"Failed to get stream info for {camera.id}: {e}")

    return None