"""
Camera Watchdog - Camera Connection Checks
Handles ping, RTSP connection, and frame capture.
"""
import socket
import subprocess
import time
import logging
from typing import Optional, Tuple
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
    frame: Optional[np.ndarray] = None
    error: str = ""
    latency_ms: float = 0.0


def ping_camera(ip: str, timeout: float = 2.0) -> PingResult:
    """Ping camera IP address."""
    start = time.time()
    try:
        # Try ICMP ping first (requires root)
        result = subprocess.run(
            ['ping', '-c', '1', '-W', str(int(timeout)), ip],
            capture_output=True,
            timeout=timeout + 1
        )
        if result.returncode == 0:
            # Parse latency from output
            output = result.stdout.decode()
            for line in output.split('\n'):
                if 'time=' in line:
                    latency_str = line.split('time=')[1].split(' ')[0]
                    try:
                        latency = float(latency_str)
                        return PingResult(True, latency)
                    except:
                        pass
            return PingResult(True, (time.time() - start) * 1000)
    except subprocess.TimeoutExpired:
        pass
    except PermissionError:
        # Fall back to TCP ping
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
        except socket.timeout:
            return PingResult(False, 0, "timeout")
        except ConnectionRefusedError:
            return PingResult(False, 0, "connection_refused")
        except Exception as e:
            return PingResult(False, 0, str(e))

    return PingResult(False, 0, "all_ports_failed")


def check_rtsp_connection(camera: CameraConfig, username: str = None, password: str = None) -> RTSPResult:
    """Check RTSP connection and try to capture a frame."""
    start = time.time()
    rtsp_url = build_rtsp_url(camera, username, password)

    try:
        # Use ffprobe to check stream
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
        latency_ms = (time.time() - start) * 1000

        if result.returncode != 0:
            stderr = result.stderr.decode('utf-8', errors='ignore')
            if '401' in stderr or 'Unauthorized' in stderr:
                return RTSPResult(False, CheckResult.AUTH_FAILED, error="Authentication failed", latency_ms=latency_ms)
            elif '404' in stderr or 'Not Found' in stderr:
                return RTSPResult(False, CheckResult.NOT_FOUND, error="Stream not found", latency_ms=latency_ms)
            elif 'timeout' in stderr.lower():
                return RTSPResult(False, CheckResult.TIMEOUT, error="Connection timeout", latency_ms=latency_ms)
            elif 'Connection refused' in stderr:
                return RTSPResult(False, CheckResult.CONNECTION_REFUSED, error="Connection refused", latency_ms=latency_ms)
            else:
                return RTSPResult(False, CheckResult.ERROR, error=stderr[:200], latency_ms=latency_ms)

        # Stream info available, now try to capture a frame
        frame = capture_frame(camera, username, password)
        if frame is not None:
            return RTSPResult(True, CheckResult.SUCCESS, frame=frame, latency_ms=latency_ms)
        else:
            return RTSPResult(False, CheckResult.ERROR, error="Failed to capture frame", latency_ms=latency_ms)

    except subprocess.TimeoutExpired:
        return RTSPResult(False, CheckResult.TIMEOUT, error="ffprobe timeout", latency_ms=(time.time() - start) * 1000)
    except Exception as e:
        return RTSPResult(False, CheckResult.ERROR, error=str(e), latency_ms=(time.time() - start) * 1000)


def capture_frame(camera: CameraConfig, username: str = None, password: str = None) -> Optional[np.ndarray]:
    """Capture a single frame from RTSP stream using ffmpeg."""
    rtsp_url = build_rtsp_url(camera, username, password)

    try:
        cmd = [
            'ffmpeg',
            '-rtsp_transport', camera.rtsp_transport,
            '-i', rtsp_url,
            '-vframes', '1',
            '-f', 'image2pipe',
            '-vcodec', 'mjpeg',
            '-q:v', '2',
            '-'
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=camera.rtsp_timeout + 5)
        if result.returncode == 0 and result.stdout:
            # Decode JPEG from stdout
            nparr = np.frombuffer(result.stdout, np.uint8)
            frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            return frame
    except subprocess.TimeoutExpired:
        logger.warning(f"Frame capture timeout for {camera.id}")
    except Exception as e:
        logger.warning(f"Frame capture error for {camera.id}: {e}")

    return None


def get_stream_info(camera: CameraConfig, username: str = None, password: str = None) -> Optional[dict]:
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
            if 'streams' in data and data['streams']:
                stream = data['streams'][0]
                # Parse frame rate
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