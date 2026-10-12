"""
Camera Watchdog v2 - Telegram Bot (interactive)
Inline commands поверх моніторингу:
  /status        — зведення всіх камер
  /shot <id>     — снапшот камери в чат
  /test          — перевірити всі камери, надіслати сітку результатів
  /mute 60       — тихо 60 хв (global snooze)
  /digest        — дайджест- повідомлення зараз
Inline-кнопки під тривогами: [📸 Снапшот] [🔕 Хвилин на 60]
"""
from __future__ import annotations

import asyncio
import logging
import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from modules.camera_check import ping_camera, check_rtsp_connection, capture_frame
from modules.image_check import ImageAnalyzer

logger = logging.getLogger(__name__)


class WatchdogBot:
    def __init__(self, watchdog):
        self.wd = watchdog
        self.config = watchdog.config
        self.tg = self.config.telegram
        self.app: Application | None = None
        self.muted_until: float = 0

    # ---------- auth ----------

    def _authed(self, user_id: int | str) -> bool:
        if not self.tg.allowed_users:
            return False   # не налаштовано = тільки chat_id owner
        return str(user_id) in self.tg.allowed_users or str(user_id) == str(self.tg.chat_id)

    # ---------- command handlers ----------

    async def cmd_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authed(update.effective_user.id):
            return
        lines = []
        for cam_id, st in self.wd.state_manager.get_all_statuses().items():
            cam = self.wd.config.get_camera(cam_id)
            name = cam.name if cam else cam_id
            emoji = {"online": "🟢", "offline": "🔴", "image_problem": "⚠️", "unknown": "⚫"}.get(
                st.state.value, "⚫")
            down = f", простій {int(st.total_downtime_seconds)}с" if st.total_downtime_seconds else ""
            lines.append(f"{emoji} {name} — {st.state.value}{down}")
        text = "📹 <b>Camera Watchdog</b>\n\n" + "\n".join(lines) + (
            f"\n\n🔇 до {time.strftime('%H:%M', time.localtime(self.muted_until))}"
            if self.muted_until > time.time() else "")
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def cmd_shot(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authed(update.effective_user.id):
            return
        if not ctx.args:
            # список камер
            names = "\n".join(f"• {c.id} — {c.name}" for c in self.wd.config.get_enabled_cameras())
            await update.message.reply_text(f"/shot <camera_id>\n\n{names}")
            return
        cam_id = ctx.args[0]
        camera = self.wd.config.get_camera(cam_id)
        if not camera:
            await update.message.reply_text(f"❌ нема камери {cam_id}")
            return
        msg = await update.message.reply_text(f"📸 {camera.name}: граблю кадр…")
        loop = asyncio.get_running_loop()
        frame = await loop.run_in_executor(
            None, capture_frame, camera, self.wd.username, self.wd.password)
        if frame is None:
            await msg.edit_text(f"❌ {camera.name}: кадр не отримано (камера offline?)")
            return
        # відправити фото
        import cv2
        import tempfile, os
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        cv2.imwrite(tmp.name, frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
        analyzer = self.wd.image_analyzer
        result = await loop.run_in_executor(None, analyzer.analyze_frame, frame, camera)
        await update.message.reply_photo(
            photo=open(tmp.name, 'rb'),
            caption=(f"📹 <b>{camera.name}</b> ({camera.ip})\n"
                     f"Аналіз: {result.issue.value} — {result.details}"),
            parse_mode=ParseMode.HTML)
        os.unlink(tmp.name)
        await msg.delete()

    async def cmd_test(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authed(update.effective_user.id):
            return
        msg = await update.message.reply_text("🔄 Тестую всі камери…")
        results = []
        loop = asyncio.get_running_loop()
        for camera in self.wd.config.get_enabled_cameras():
            ping_result = await loop.run_in_executor(None, ping_camera, camera.ip)
            if not ping_result.success:
                results.append((camera, "🔴 offline", None))
                continue
            rtsp = await loop.run_in_executor(
                None, check_rtsp_connection, camera, self.wd.username, self.wd.password)
            if not rtsp.success:
                results.append((camera, f"⚠️ RTSP fail ({rtsp.result_type.value})", None))
                continue
            frame = rtsp.frame or await loop.run_in_executor(
                None, capture_frame, camera, self.wd.username, self.wd.password)
            if frame is None:
                results.append((camera, "⚠️ кадр не отримано", None))
                continue
            analysis = await loop.run_in_executor(None, self.wd.image_analyzer.analyze_frame, frame, camera)
            emoji = "🟢" if analysis.issue.value == "none" else "⚠️"
            results.append((camera, f"{emoji} OK ({analysis.issue.value})", frame))
        # зведення
        lines = []
        for camera, text_res, _f in results:
            lines.append(f"{text_res} {camera.name}")
        await msg.edit_text("📹 <b>Тест завершено</b>\n\n" + "\n".join(lines), parse_mode=ParseMode.HTML)
        # снапшоти слідом
        for camera, _t, frame in results:
            if frame is not None:
                import cv2, tempfile, os
                tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                cv2.imwrite(tmp.name, frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                with open(tmp.name, 'rb') as ph:
                    await update.message.reply_photo(photo=ph, caption=f"📷 {camera.name}")
                os.unlink(tmp.name)
                await asyncio.sleep(0.4)   # rate-limit

    async def cmd_mute(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authed(update.effective_user.id):
            return
        mins = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 60
        self.muted_until = time.time() + mins * 60
        await update.message.reply_text(f"🔇 Тривоги заглушено на {mins} хв")

    async def cmd_digest(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authed(update.effective_user.id):
            return
        cams = self.wd.config.get_enabled_cameras()
        self.wd.notifier.send_digest(cams, self.wd.state_manager.get_all_statuses())
        await update.message.reply_text("✅ Дайджест відправлений")

    async def cmd_stolen(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """v2: вручну позначити камеру як вкрадену (зупинити моніторинг)."""
        if not self._authed(update.effective_user.id):
            return
        if not ctx.args:
            await update.message.reply_text("/stolen <camera_id>")
            return
        cam_id = ctx.args[0]
        status = self.wd.state_manager.get_status(cam_id)
        if not status:
            await update.message.reply_text(f"❌ нема камери {cam_id}")
            return
        status.mark_stolen()
        self.wd.state_manager.flush()
        await update.message.reply_text(f"🚨 {cam_id} → STOLEN. Моніторинг зупинено. /resume {cam_id} щоб відновити")

    async def cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """v2: відновити моніторинг украденої камери."""
        if not self._authed(update.effective_user.id):
            return
        if not ctx.args:
            # список камер в STOLEN
            stolen = [cid for cid, st in self.wd.state_manager.get_all_statuses().items()
                      if st.state.value == "stolen"]
            await update.message.reply_text(
                "/resume <camera_id>\n\n🚨 STOLEN: " + (", ".join(stolen) if stolen else "нема"))
            return
        cam_id = ctx.args[0]
        status = self.wd.state_manager.get_status(cam_id)
        if not status:
            await update.message.reply_text(f"❌ нема камери {cam_id}")
            return
        status.resume()
        self.wd.state_manager.flush()
        await update.message.reply_text(f"⚡ {cam_id} → моніторинг відновлено")

    # ---------- v2.1: ROI motion ----------

    async def cmd_roi(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """
        /roi <cam_id>            — кадр з сіткою 10% (виміряй зону щитка)
        /roi <cam_id> x1 y1 x2 y2 [name]  — кадр з червоним прямокутником зони
        """
        if not self._authed(update.effective_user.id):
            return
        if not ctx.args:
            await update.message.reply_text(
                "/roi <cam_id> — кадр з сіткою 10% (шкала 0-100)\n"
                "/roi <cam_id> x1 y1 x2 y2 [name] — перевірка зони (червоний прямокутник)\n\n"
                "Приклад: /roi cam70-floor 45 20 65 75 shchitok")
            return
        cam_id = ctx.args[0]
        camera = self.wd.config.get_camera(cam_id)
        if not camera:
            await update.message.reply_text(f"❌ нема камери {cam_id}")
            return
        msg = await update.message.reply_text("📸 граблю…")
        loop = asyncio.get_running_loop()
        frame = await loop.run_in_executor(
            None, capture_frame, camera, self.wd.username, self.wd.password)
        await msg.delete()
        if frame is None:
            await update.message.reply_text(f"❌ {camera.name}: кадр не отримано")
            return
        from modules.roi_motion import draw_region_grid, draw_region_box, parse_region_args, MotionRegion
        if len(ctx.args) >= 5:
            region = parse_region_args(ctx.args[1:5], name=ctx.args[5] if len(ctx.args) > 5 else "zone1")
            if not region:
                await update.message.reply_text("❌ координати: x1 y1 x2 y2 у % (0-100), x1<x2, y1<y2")
                return
            vis = await loop.run_in_executor(None, draw_region_box, frame, region)
        else:
            vis = await loop.run_in_executor(None, draw_region_grid, frame, 10)
        import cv2, tempfile, os
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        cv2.imwrite(tmp.name, vis, [cv2.IMWRITE_JPEG_QUALITY, 85])
        with open(tmp.name, 'rb') as ph:
            await update.message.reply_photo(photo=ph, caption=f"📐 {camera.name}")
        os.unlink(tmp.name)

    async def cmd_motion(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """
        /motion — статус усіх motion-зон
        /motion check <cam_id> — примусова перевірка зон камери зараз
        """
        if not self._authed(update.effective_user.id):
            return
        cams_with_zones = [c for c in self.wd.config.get_enabled_cameras() if c.motion_regions]
        if not cams_with_zones:
            await update.message.reply_text(
                "Моушн-зон нема. Додай у config.yaml:\n\n"
                "motion:\n  enabled: true\n\n"
                "  і камері:\n  motion_regions:\n    - name: shchitok\n"
                "      x1: 45\n      y1: 20\n      x2: 65\n      y2: 75")
            return
        if ctx.args and ctx.args[0] == "check":
            cam_id = ctx.args[1] if len(ctx.args) > 1 else ""
            camera = next((c for c in cams_with_zones if c.id == cam_id), None)
            if not camera:
                await update.message.reply_text(
                    "❌ камера без зон: " + cam_id + "\nЗони на: " +
                    ", ".join(c.id for c in cams_with_zones))
                return
            msg = await update.message.reply_text("🔄 перевіряю зони…")
            loop = asyncio.get_running_loop()
            frame = await loop.run_in_executor(
                None, capture_frame, camera, self.wd.username, self.wd.password)
            # скинути cooldown щоб перевірка була миттєвою
            if frame is not None:
                triggers = await loop.run_in_executor(
                    None, self.wd.motion_detector.check_frame, camera.id, frame)
                if not triggers:
                    await msg.edit_text(f"✅ {camera.name}: зони спокійні")
                else:
                    for t in triggers:
                        txt = (f"🎯 {camera.name}/{t['region']}: "
                               f"{t['change_pct']}% (поріг {t['threshold_pct']}%)"
                               + (" — алерт надіслано" if t['cooldown_ok'] else " — cooldown"))
                        await msg.edit_text(txt) if t is triggers[0] else await update.message.reply_text(txt)
                        if t['snapshot']:
                            import tempfile, os
                            with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
                                tmp.write(t['snapshot'])
                            with open(tmp.name, 'rb') as ph:
                                await update.message.reply_photo(photo=ph)
                            os.unlink(tmp.name)
            else:
                await msg.edit_text(f"❌ кадр не отримано з {cam_id}")
            return
        # статус зон
        lines = []
        for c in cams_with_zones:
            names = ", ".join(r.name for r in c.motion_regions)
            lines.append(f"• {c.id}: [{names}]")
        await update.message.reply_text(
            "🎯 <b>Motion-зони</b>\n\n" + "\n".join(lines) +
            "\n\n/motion check <cam_id> — перевірка зараз", parse_mode=ParseMode.HTML)

    # ---------- inline button handler ----------

    async def on_button(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authed(update.callback_query.from_user.id):
            await update.callback_query.answer("⛔ ні", show_alert=False)
            return
        q = update.callback_query
        action, _, cam_id = (q.data or "").partition(":")

        # v2: theft-prompt відповіді
        if action == "stolen":
            await q.answer("🚨 позначено: вкрадена")
            status = self.wd.state_manager.get_status(cam_id)
            if status:
                status.mark_stolen()
                self.wd.state_manager.flush()
            await q.edit_message_text(
                f"{q.message.text}\n\n🚨 → <b>STOLEN</b> (моніторинг зупинено до /resume {cam_id})",
                parse_mode=ParseMode.HTML)
            return
        if action == "resume":
            await q.answer("⚡ моніторинг відновлено")
            status = self.wd.state_manager.get_status(cam_id)
            if status:
                status.resume()
                self.wd.state_manager.flush()
            await q.edit_message_text(
                f"{q.message.text}\n\n⚡ → <b>РЕСЮМ — моніторинг продовжується</b>",
                parse_mode=ParseMode.HTML)
            return

        if action == "shot":
            await q.answer("📸 граблю…")
            camera = self.wd.config.get_camera(cam_id)
            if not camera:
                return
            loop = asyncio.get_running_loop()
            frame = await loop.run_in_executor(
                None, capture_frame, camera, self.wd.username, self.wd.password)
            if frame is None:
                await q.message.reply_text(f"❌ {camera.name}: кадр не отримано")
                return
            import cv2, tempfile, os
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            cv2.imwrite(tmp.name, frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
            with open(tmp.name, 'rb') as ph:
                await q.message.reply_photo(photo=ph, caption=f"📷 {camera.name}")
            os.unlink(tmp.name)
        elif action == "mute":
            # snooze per camera — просто answer і глушимо
            await q.answer(f"🔕 {cam_id} заглушено")
            key = f"{cam_id}:snoosze"
            self.wd.notifier.alert_states[key] = type(self.wd.notifier).alert_states.get(key) or None
            # простіше: ставимо cooldown в state
            self.wd.notifier._record_alert(cam_id, "offline")
        elif action == "ok":
            await q.answer("✅ позначено як оброблене")
            self.wd.notifier._clear_alert(cam_id, "image_" + "black_frame")  # обидві типові
            self.wd.notifier._clear_alert(cam_id, "online")

    # ---------- alert-side inline buttons ----------

    def build_alert_keyboard(self, camera_id: str):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📸 Снапшот", callback_data=f"shot:{camera_id}"),
             InlineKeyboardButton("🔕 Mute", callback_data=f"mute:{camera_id}"),
             InlineKeyboardButton("✅ Ок", callback_data=f"ok:{camera_id}")]
        ])

    # ---------- lifecycle ----------

    async def start(self):
        if not self.config.telegram.enabled or not self.tg.bot_token:
            logger.info("Inline bot disabled (telegram.enabled=false or no token)")
            return None
        self.app = Application.builder().token(self.tg.bot_token).build()
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("shot", self.cmd_shot))
        self.app.add_handler(CommandHandler("test", self.cmd_test))
        self.app.add_handler(CommandHandler("mute", self.cmd_mute))
        self.app.add_handler(CommandHandler("digest", self.cmd_digest))
        self.app.add_handler(CommandHandler("stolen", self.cmd_stolen))
        self.app.add_handler(CommandHandler("resume", self.cmd_resume))
        self.app.add_handler(CommandHandler("roi", self.cmd_roi))
        self.app.add_handler(CommandHandler("motion", self.cmd_motion))
        self.app.add_handler(CallbackQueryHandler(self.on_button))
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        logger.info("Telegram inline bot started (polling)")
        return self.app

    async def stop(self):
        if self.app:
            try:
                await self.app.updater.stop()
                await self.app.stop()
                await self.app.shutdown()
            except Exception as e:
                logger.warning(f"bot stop: {e}")
            self.app = None
            logger.info("Telegram inline bot stopped")