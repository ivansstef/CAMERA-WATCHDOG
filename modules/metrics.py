"""
Camera Watchdog - Prometheus Metrics (v2)
Exposes /metrics for scraping; uptime, latency, state counters per camera.
"""
import asyncio
import logging

from prometheus_client import CollectorRegistry, Gauge, start_http_server

logger = logging.getLogger(__name__)

registry = CollectorRegistry()

camera_state = Gauge(
    "camera_state", "Camera state: 0=unknown, 1=online, 2=offline, 3=image_problem",
    ["camera_id", "camera_name", "ip"], registry=registry)
camera_ping_latency = Gauge(
    "camera_ping_latency_ms", "Last ping latency (ms)",
    ["camera_id"], registry=registry)
camera_rtsp_latency = Gauge(
    "camera_rtsp_latency_ms", "Last RTSP check latency (ms)",
    ["camera_id"], registry=registry)
camera_downtime_total = Gauge(
    "camera_downtime_seconds_total", "Total downtime accumulated (s)",
    ["camera_id"], registry=registry)
camera_last_success_ts = Gauge(
    "camera_last_success_timestamp_seconds", "Last successful check timestamp",
    ["camera_id"], registry=registry)


def update_metrics(watchdog):
    """Refresh gauge values from current watchdog state."""
    state_map = {"unknown": 0, "online": 1, "offline": 2, "image_problem": 3}
    for cam_id, status in watchdog.state_manager.get_all_statuses().items():
        camera = watchdog.config.get_camera(cam_id)
        name = camera.name if camera else cam_id
        ip = camera.ip if camera else "unknown"
        camera_state.labels(camera_id=cam_id, camera_name=name, ip=ip).set(
            state_map.get(status.state.value, 0))
        camera_ping_latency.labels(camera_id=cam_id).set(status.last_ping_latency)
        camera_rtsp_latency.labels(camera_id=cam_id).set(status.last_rtsp_latency)
        camera_downtime_total.labels(camera_id=cam_id).set(status.total_downtime_seconds)
        camera_last_success_ts.labels(camera_id=cam_id).set(status.last_success)


async def metrics_refresh_loop(watchdog, interval_s: int = 15):
    """Periodically refresh metrics from live state."""
    while True:
        try:
            await asyncio.to_thread(update_metrics, watchdog)
        except Exception as e:
            logger.warning(f"metrics refresh error: {e}")
        await asyncio.sleep(interval_s)


async def start_metrics_server(watchdog, port: int = 8000):
    """Start Prometheus scrape endpoint (blocking-in-thread start + refresh loop)."""
    def _start():
        start_http_server(port, registry=registry)
        logger.info(f"Prometheus metrics listening on :{port}/metrics")

    await asyncio.to_thread(_start)
    asyncio.create_task(metrics_refresh_loop(watchdog))