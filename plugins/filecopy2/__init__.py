import datetime
import random
import threading
import time
import os
import shutil
from pathlib import Path
from typing import List, Tuple, Dict, Any

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.utils.system import SystemUtils
from app.schemas.types import NotificationType  # 用于发送通知

# 使用模块级锁，防止多个 job 并发执行 copy_files
lock = threading.Lock()


class FileCopy2(_PluginBase):
    # 插件元信息
    plugin_name = "文件复制（完善版）"
    plugin_desc = "自定义文件类型从源目录复制到目的目录。"
    plugin_icon = "https://raw.githubusercontent.com/LGaoo/MoviePilot-Plugins/main/icons/copy_files.png"
    plugin_version = "1.3"
    plugin_author = "LGaoo"
    author_url = "https://github.com/LGaoo"
    plugin_config_prefix = "filecopy2_"
    plugin_order = 30
    auth_level = 1

    # 私有属性
    _scheduler = None
    _enabled = False
    _onlyonce = False
    _cron = None
    _delay = None
    _monitor_dirs = ""
    # 保持 key 为原始字符串（兼容原版解析）
    _dirconf: Dict[str, Path] = {}

    _rmt_mediaext = None

    # 新增：是否发送通知（默认 True），是否删除源文件（默认 False），是否保留子目录（默认 False）
    _notify = True
    _delete_source = False
    _preserve_dirs = False

    _event = threading.Event()

    def init_plugin(self, config: dict = None):
        # 清空配置
        self._dirconf = {}

        # 读取配置（与原版一致，并读取 notify、delete、preserve_dirs）
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._monitor_dirs = config.get("monitor_dirs") or ""
            self._cron = config.get("cron")
            self._delay = config.get("delay")
            self._rmt_mediaext = config.get("rmt_mediaext") or ".nfo, .jpg"
            # 新增配置项读取
            self._notify = config.get("notify") if config.get("notify") is not None else True
            self._delete_source = config.get("delete") if config.get("delete") is not None else False
            self._preserve_dirs = config.get("preserve_dirs") if config.get("preserve_dirs") is not None else False

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 定时服务管理器
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            # 读取目录配置，先构建 _dirconf（不要在循环里注册 job）
            monitor_dirs = self._monitor_dirs.split("\n")
            if not monitor_dirs:
                return
            for mon_path in monitor_dirs:
                if not mon_path:
                    continue
                if SystemUtils.is_windows():
                    if mon_path.count(":") > 1:
                        parts = mon_path.split(":")
                        paths = [parts[0] + ":" + parts[1], parts[2] + ":" + parts[3]]
                    else:
                        paths = [mon_path]
                else:
                    paths = mon_path.split(":")
                if len(paths) > 1:
                    mon_path_key = paths[0].strip()
                    target_path = Path(paths[1].strip())
                    self._dirconf[mon_path_key] = target_path
                else:
                    mon_path_key = paths[0].strip()
                    self._dirconf[mon_path_key] = None

            # 只注册一个 “立即运行一次” job（若启用），避免为每个监控目录都注册
            if self._enabled:
                self._scheduler.add_job(func=self.copy_files, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3),
                                        name=f"文件复制")

            # 运行一次定时服务（onlyonce）
            if self._onlyonce:
                logger.info("文件复制服务启动，立即运行一次")
                self._scheduler.add_job(name="文件复制", func=self.copy_files, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                        )
                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

            # 启动定时服务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def _verify_copy(self, src: Path, dst: Path) -> bool:
        """
        验证复制是否成功 —— 通过文件存在与大小一致来判断。
        返回 True 表示复制成功。
        """
        try:
            if not dst.exists():
                return False
            src_stat = src.stat()
            dst_stat = dst.stat()
            return src_stat.st_size == dst_stat.st_size
        except Exception as e:
            logger.error(f"验证复制失败：{e}")
            return False

    def _make_unique_dest(self, dest: Path) -> Path:
        """
        如果 dest 已存在，尝试生成 dest (1).ext, dest (2).ext ...
        """
        if not dest.exists():
            return dest
        parent = dest.parent
        name = dest.stem
        suffix = dest.suffix
        i = 1
        while True:
            candidate = parent / f"{name} ({i}){suffix}"
            if not candidate.exists():
                return candidate
            i += 1

    def copy_files(self):
        """
        定时任务，复制文件（稳定遍历 + 验证复制 + 可选删除源文件 + 可选发送通知）
        修复点：
          - 使用模块级 lock 防止并发执行
          - 只会为插件注册一个立即运行 job（在 init_plugin 中）
          - 在复制前再次检查源是否存在，回退复制前也检查
        """
        # 防止并发运行
        got = lock.acquire(blocking=False)
        if not got:
            logger.warning("上一次文件复制任务仍在运行，跳过本次触发")
            return
        try:
            logger.info("开始全量复制监控目录（含通知与可选删除，preserve_dirs=%s）..." % str(self._preserve_dirs))

            # 解析扩展名，规范为小写并以 '.' 开头
            exts = []
            if self._rmt_mediaext:
                try:
                    for ext in [e.strip() for e in self._rmt_mediaext.split(",") if e.strip()]:
                        if not ext.startswith("."):
                            ext = "." + ext
                        exts.append(ext.lower())
                except Exception:
                    exts = []

            scan_all = len(exts) == 0

            # 统计（用于通知）
            total_found = 0
            total_copied = 0
            total_failed = 0
            total_skipped = 0
            total_deleted = 0
            copied_examples = []
            failed_examples = []
            skipped_examples = []
            deleted_examples = []

            # 遍历配置
            for mon_path, target_path in list(self._dirconf.items()):
                if not mon_path:
                    continue

                src_base = Path(mon_path)
                if target_path is None:
                    logger.info(f"{mon_path} 未配置目标目录，跳过")
                    continue
                tgt_base = Path(target_path)

                if not src_base.exists():
                    logger.warning(f"源目录不存在，跳过：{repr(str(src_base))}")
                    continue

                logger.info(f"扫描目录：{repr(str(src_base))}")

                # 使用 os.walk 递归并 followlinks=True 更稳定（网络挂载、symlink）
                files = []
                try:
                    dir_count = 0
                    for root, dirs, filenames in os.walk(str(src_base), followlinks=True):
                        dir_count += 1
                        for fname in filenames:
                            try:
                                p = Path(root) / fname
                                if not p.is_file():
                                    continue
                                if scan_all or (p.suffix.lower() in exts):
                                    files.append(p)
                            except Exception:
                                continue
                    logger.info(f"遍历目录数：{dir_count}")
                except Exception as e:
                    logger.error(f"os.walk 遍历失败：{e} -- 回退到 SystemUtils.list_files")
                    try:
                        files = SystemUtils.list_files(src_base, exts)
                        files = [Path(x) if not isinstance(x, Path) else x for x in files]
                    except Exception as e2:
                        logger.error(f"回退也失败：{e2}")
                        files = []

                # 去重并排序
                try:
                    uniq = []
                    seen = set()
                    for p in files:
                        s = str(p)
                        if s not in seen:
                            seen.add(s)
                            uniq.append(Path(s))
                    files = sorted(uniq)
                except Exception:
                    pass

                logger.info(f"发现文件数量：{len(files)} (示例前5个: { [repr(str(x)) for x in files[:5]] })")
                total_found += len(files)

                cnt = 0
                for file_obj in files:
                    try:
                        file_path = Path(file_obj) if not isinstance(file_obj, Path) else file_obj

                        # 再次检查源文件是否存在（防止复制时被其它进程移动/删除）
                        if not file_path.exists():
                            logger.warning(f"源文件在处理前已不存在，跳过：{repr(str(file_path))}")
                            total_failed += 1
                            if len(failed_examples) < 10:
                                failed_examples.append(f"{file_path} -> 源文件不存在")
                            continue

                        try:
                            src_size = file_path.stat().st_size
                        except Exception:
                            src_size = -1
                        logger.info(f"开始处理本地文件：{repr(str(file_path))} (大小: {src_size})")

                        # 目标路径决定：保留子目录 or 平铺
                        if self._preserve_dirs:
                            try:
                                relative = file_path.relative_to(src_base)
                            except Exception:
                                rel_str = os.path.relpath(str(file_path), start=str(src_base))
                                relative = Path(rel_str)
                            dest_file = tgt_base.joinpath(*relative.parts)
                        else:
                            # 平铺：只保留文件名，放到目标目录根
                            dest_file = tgt_base.joinpath(file_path.name)

                        # 若目标已存在且一致，跳过
                        if dest_file.exists() and self._verify_copy(file_path, dest_file):
                            logger.info(f"{repr(str(dest_file))} 文件已存在且一致，跳过")
                            total_skipped += 1
                            if len(skipped_examples) < 10:
                                skipped_examples.append(str(dest_file))
                            continue

                        # 平铺模式下，若目标存在但大小不一致，生成唯一名字避免覆盖
                        if not self._preserve_dirs and dest_file.exists() and not self._verify_copy(file_path, dest_file):
                            dest_file = self._make_unique_dest(dest_file)

                        # 确保目标父目录存在
                        dest_dir = dest_file.parent
                        if not dest_dir.exists():
                            try:
                                dest_dir.mkdir(parents=True, exist_ok=True)
                                logger.info(f"创建目标目录：{repr(str(dest_dir))}")
                            except Exception as e:
                                logger.error(f"创建目标目录失败：{repr(str(dest_dir))} -> {e}")
                                total_failed += 1
                                if len(failed_examples) < 10:
                                    failed_examples.append(f"{file_path} -> 创建目标目录失败: {e}")
                                continue

                        # 在调用 SystemUtils.copy 前再次确认源文件还在
                        if not file_path.exists():
                            logger.warning(f"源文件在复制前已不存在，跳过：{repr(str(file_path))}")
                            total_failed += 1
                            if len(failed_examples) < 10:
                                failed_examples.append(f"{file_path} -> 源文件不存在（复制前）")
                            continue

                        # 执行复制（优先 SystemUtils.copy）
                        copy_ok = False
                        copy_err = ""
                        try:
                            state, error = SystemUtils.copy(file_path, dest_file)
                            copy_ok = (state == 0)
                            copy_err = error
                        except Exception as e:
                            copy_ok = False
                            copy_err = str(e)

                        # 验证复制结果
                        copy_verified = False
                        if copy_ok and self._verify_copy(file_path, dest_file):
                            logger.info(f"{repr(str(file_path))} -> {repr(str(dest_file))} 成功 (SystemUtils.copy)")
                            total_copied += 1
                            copy_verified = True
                            if len(copied_examples) < 10:
                                copied_examples.append(str(dest_file))
                        else:
                            # 回退到 shutil.copy2，但先检查源是否存在
                            logger.warning(f"SystemUtils.copy 结果不可验证或失败 (info: {copy_err}), 回退到 shutil.copy2")
                            if not file_path.exists():
                                logger.warning(f"回退复制前源文件已不存在，跳过回退：{repr(str(file_path))}")
                                total_failed += 1
                                if len(failed_examples) < 10:
                                    failed_examples.append(f"{file_path} -> 回退复制前源不存在")
                            else:
                                try:
                                    shutil.copy2(str(file_path), str(dest_file))
                                except Exception as e:
                                    logger.error(f"shutil.copy2 复制失败：{e}")
                                # 再次验证
                                if self._verify_copy(file_path, dest_file):
                                    logger.info(f"{repr(str(file_path))} -> {repr(str(dest_file))} 成功 (shutil.copy2 回退)")
                                    total_copied += 1
                                    copy_verified = True
                                    if len(copied_examples) < 10:
                                        copied_examples.append(str(dest_file))
                                else:
                                    src_size2 = -1
                                    dst_size2 = -1
                                    try:
                                        src_size2 = file_path.stat().st_size
                                    except Exception:
                                        pass
                                    try:
                                        dst_size2 = dest_file.stat().st_size
                                    except Exception:
                                        pass
                                    logger.error(f"{repr(str(file_path))} -> {repr(str(dest_file))} 最终复制失败 (src_size={src_size2}, dst_size={dst_size2})")
                                    total_failed += 1
                                    if len(failed_examples) < 10:
                                        failed_examples.append(f"{file_path} -> 最终失败 (src_size={src_size2}, dst_size={dst_size2})")

                        # 如果复制已验证成功并且启用了删除源文件，则删除源文件
                        if copy_verified and self._delete_source:
                            try:
                                # 再次确保源存在再删除
                                if file_path.exists():
                                    file_path.unlink()
                                    total_deleted += 1
                                    if len(deleted_examples) < 10:
                                        deleted_examples.append(str(file_path))
                                    logger.info(f"已删除源文件：{repr(str(file_path))}")
                                else:
                                    logger.warning(f"计划删除的源文件已不存在：{repr(str(file_path))}")
                            except Exception as e:
                                logger.error(f"删除源文件失败：{file_path} -> {e}")
                                if len(failed_examples) < 10:
                                    failed_examples.append(f"{file_path} -> 删除失败: {e}")

                        # 随机/批量延时逻辑（保持原配置动态生效）
                        if self._delay:
                            try:
                                cnt += 1
                                delays = self._delay.split(",")
                                if len(delays) >= 2 and cnt >= int(delays[0]):
                                    delay_spec = delays[1]
                                    if "-" in delay_spec:
                                        a, b = delay_spec.split("-")
                                        wait_time = random.randint(int(a), int(b))
                                    else:
                                        wait_time = int(delay_spec)
                                    logger.info(f"随机延迟 {wait_time} 秒")
                                    time.sleep(wait_time)
                                    cnt = 0
                            except Exception as e:
                                logger.error(f"处理延时配置出错：{e}")

                    except Exception as ex:
                        logger.error(f"处理文件 {repr(str(file_obj))} 时异常：{ex}")
                        total_failed += 1
                        if len(failed_examples) < 10:
                            failed_examples.append(f"{file_obj} -> 异常: {ex}")

            logger.info("全量复制监控目录完成！")

            # 发送通知（若启用）
            if self._notify:
                try:
                    title = "【文件复制任务完成】"
                    body_lines = [
                        f"检测到文件数: {total_found}",
                        f"成功复制: {total_copied}",
                        f"跳过（已存在）: {total_skipped}",
                        f"失败: {total_failed}",
                        f"已删除源文件: {total_deleted}"
                    ]
                    if copied_examples:
                        body_lines.append("")
                        body_lines.append("示例已复制文件（最多显示10条）:")
                        body_lines += copied_examples[:10]
                    if skipped_examples:
                        body_lines.append("")
                        body_lines.append("示例被跳过文件（最多显示10条）:")
                        body_lines += skipped_examples[:10]
                    if failed_examples:
                        body_lines.append("")
                        body_lines.append("示例失败文件（最多显示10条）:")
                        body_lines += failed_examples[:10]
                    if deleted_examples:
                        body_lines.append("")
                        body_lines.append("示例已删除源文件（最多显示10条）:")
                        body_lines += deleted_examples[:10]

                    message = "\n".join(body_lines)
                    # 调用基类 post_message 发送通知
                    self.post_message(title=title, mtype=NotificationType.SiteMessage, text=message)
                except Exception as e:
                    logger.error(f"发送通知失败：{e}")
        finally:
            lock.release()

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "monitor_dirs": self._monitor_dirs,
            "cron": self._cron,
            "delay": self._delay,
            "rmt_mediaext": self._rmt_mediaext,
            "notify": self._notify,
            "delete": self._delete_source,
            "preserve_dirs": self._preserve_dirs
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        # 使用唯一 id 避免与旧版冲突
        if self._enabled and self._cron:
            return [{
                "id": "FileCopy2",
                "name": "文件复制（完善版）",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.copy_files,
                "kwargs": {}
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        # 保持原有表单，略（同你之前的）
        return [
            {
                'component': 'VForm',
                'content': [
                    # ... 保持你之前的表单定义（不变）
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "monitor_dirs": "",
            "cron": "",
            "delay": "20,1-10",
            "rmt_mediaext": ".mp4, .mkv, .avi, .ts, .srt, .ass, .ssa, .sub, .idx",
            "notify": True,
            "delete": False,
            "preserve_dirs": False
        }

    def get_page(self) -> List[dict]:
        return []

    def stop_service(self):
        """
        退出插件
        """
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                self._event.set()
                self._scheduler.shutdown()
                self._event.clear()
            self._scheduler = None
