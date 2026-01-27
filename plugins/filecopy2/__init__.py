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
from app.schemas.types import NotificationType

# 模块级锁，防止并发执行 copy_files
lock = threading.Lock()


class FileCopy2(_PluginBase):
    plugin_name = "文件复制（完善版）"
    plugin_desc = "自定义文件类型从源目录复制到目的目录。"
    plugin_icon = "https://raw.githubusercontent.com/LGaoo/MoviePilot-Plugins/main/icons/copy_files.png"
    plugin_version = "1.7"
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
    _dirconf: Dict[str, Path] = {}

    _rmt_mediaext = None

    # 新增选项（默认）
    _notify = True
    _delete_source = False
    _preserve_dirs = False

    _event = threading.Event()

    def init_plugin(self, config: dict = None):
        # 清空目录配置
        self._dirconf = {}

        # 读取配置（包含 notify/delete/preserve_dirs）
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._monitor_dirs = config.get("monitor_dirs") or ""
            self._cron = config.get("cron")
            self._delay = config.get("delay")
            self._rmt_mediaext = config.get("rmt_mediaext") or ".nfo, .jpg"
            self._notify = config.get("notify") if config.get("notify") is not None else True
            self._delete_source = config.get("delete") if config.get("delete") is not None else False
            self._preserve_dirs = config.get("preserve_dirs") if config.get("preserve_dirs") is not None else False

        # 停止现有任务以便重启
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 定时服务管理器
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            # 读取并解析监控目录配置（一次性构建 _dirconf）
            monitor_dirs = self._monitor_dirs.split("\n")
            if not monitor_dirs:
                return
            for mon_path in monitor_dirs:
                if not mon_path:
                    continue

                # Windows 路径特殊解析（保持与原版兼容）
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

            # 只注册一个立即执行 job，避免为每个监控目录重复触发
            if self._enabled:
                self._scheduler.add_job(func=self.copy_files, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3),
                                        name=f"文件复制")
            # 如果 onlyonce，则注册一次性执行并保存配置
            if self._onlyonce:
                logger.info(f"文件复制服务启动，立即运行一次 版本{self.plugin_version}")
                self._scheduler.add_job(name="文件复制", func=self.copy_files, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                        )
                self._onlyonce = False
                self.__update_config()

            # 启动定时服务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def _verify_copy(self, src: Path, dst: Path) -> bool:
        """
        通过存在与大小一致来判断复制是否成功
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
        若目标已存在，生成 name (n).ext 的唯一名字
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
        主任务：遍历、复制、校验、可选删除源、可选通知。
        使用临时文件 + os.replace 实现原子写入并尽力 fsync 确保落盘。
        在重要日志中打印当前插件版本号，便于排障。
        """
        # 防止并发运行
        got = lock.acquire(blocking=False)
        if not got:
            logger.warning("上一次文件复制任务仍在运行，跳过本次触发")
            return
        try:
            logger.info(f"开始全量复制监控目录（preserve_dirs={self._preserve_dirs}, notify={self._notify}, delete={self._delete_source}） 版本{self.plugin_version}")

            # 解析扩展名（标准化）
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

            # 统计汇总
            total_found = 0
            total_copied = 0
            total_failed = 0
            total_skipped = 0
            total_deleted = 0
            copied_examples = []
            failed_examples = []
            skipped_examples = []
            deleted_examples = []

            # 遍历每个监控项
            for mon_path, target_path in list(self._dirconf.items()):
                if not mon_path:
                    continue

                src_base = Path(mon_path)
                if target_path is None:
                    logger.info(f"{mon_path} 未配置目标目录，跳过")
                    continue
                tgt_base = Path(target_path)

                # 监控项开始日志
                logger.info(f"==== 开始处理监控项: {repr(str(src_base))} -> {repr(str(tgt_base))} ====")

                if not src_base.exists():
                    logger.warning(f"源目录不存在，跳过：{repr(str(src_base))}")
                    continue

                logger.info(f"扫描目录：{repr(str(src_base))}")

                # 收集文件（os.walk followlinks=True）
                files = []
                roots_sample = []
                try:
                    dir_count = 0
                    for root, dirs, filenames in os.walk(str(src_base), followlinks=True):
                        dir_count += 1
                        if len(roots_sample) < 20:
                            roots_sample.append(root)
                        for fname in filenames:
                            try:
                                p = Path(root) / fname
                                if not p.is_file():
                                    continue
                                if scan_all or (p.suffix.lower() in exts):
                                    files.append(p)
                            except Exception:
                                continue
                    logger.info(f"遍历目录数：{dir_count} (示例前20个根目录: {roots_sample})")
                except Exception as e:
                    logger.error(f"os.walk 遍历失败：{e} -- 回退到 SystemUtils.list_files")
                    try:
                        files = SystemUtils.list_files(src_base, exts)
                        files = [Path(x) if not isinstance(x, Path) else x for x in files]
                        logger.info(f"SystemUtils.list_files 返回数量：{len(files)} (示例前20个: {[str(x) for x in files[:20]]})")
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

                logger.info(f"发现文件数量：{len(files)} (示例前20个: {[repr(str(x)) for x in files[:20]]})")
                total_found += len(files)

                cnt = 0
                # 每个监控项的统计
                item_found = 0
                item_copied = 0
                item_failed = 0
                item_skipped = 0
                item_deleted = 0

                for file_obj in files:
                    item_found += 1
                    try:
                        file_path = Path(file_obj) if not isinstance(file_obj, Path) else file_obj

                        # 再次检查源文件是否存在
                        if not file_path.exists():
                            logger.warning(f"源文件在处理前已不存在，跳过：{repr(str(file_path))}")
                            item_failed += 1
                            total_failed += 1
                            if len(failed_examples) < 10:
                                failed_examples.append(f"{file_path} -> 源文件不存在")
                            continue

                        try:
                            src_size = file_path.stat().st_size
                        except Exception:
                            src_size = -1
                        logger.info(f"[文件] 开始处理：{repr(str(file_path))} (大小: {src_size})")

                        # 目标路径决定（保留目录 or 平铺）
                        if self._preserve_dirs:
                            try:
                                relative = file_path.relative_to(src_base)
                            except Exception:
                                rel_str = os.path.relpath(str(file_path), start=str(src_base))
                                relative = Path(rel_str)
                            dest_file = tgt_base.joinpath(*relative.parts)
                        else:
                            dest_file = tgt_base.joinpath(file_path.name)

                        logger.info(f"[映射] 源 -> 目标: {repr(str(file_path))} -> {repr(str(dest_file))}")

                        # 若目标已存在且一致，跳过
                        if dest_file.exists() and self._verify_copy(file_path, dest_file):
                            logger.info(f"{repr(str(dest_file))} 文件已存在且一致，跳过")
                            item_skipped += 1
                            total_skipped += 1
                            if len(skipped_examples) < 10:
                                skipped_examples.append(str(dest_file))
                            continue

                        # 平铺模式冲突处理：若目标存在但不一致，生成唯一名字
                        if not self._preserve_dirs and dest_file.exists() and not self._verify_copy(file_path, dest_file):
                            orig_dest = dest_file
                            dest_file = self._make_unique_dest(dest_file)
                            logger.info(f"[冲突] 目标已存在且不一致，使用唯一目标: {repr(str(orig_dest))} -> {repr(str(dest_file))}")

                        # 确保目标父目录存在
                        dest_dir = dest_file.parent
                        if not dest_dir.exists():
                            try:
                                dest_dir.mkdir(parents=True, exist_ok=True)
                                logger.info(f"创建目标目录：{repr(str(dest_dir))}")
                            except Exception as e:
                                logger.error(f"创建目标目录失败：{repr(str(dest_dir))} -> {e}")
                                item_failed += 1
                                total_failed += 1
                                if len(failed_examples) < 10:
                                    failed_examples.append(f"{file_path} -> 创建目标目录失败: {e}")
                                continue

                        # 在调用复制步骤前再次确认源仍存在
                        if not file_path.exists():
                            logger.warning(f"源文件在复制前已不存在，跳过：{repr(str(file_path))}")
                            item_failed += 1
                            total_failed += 1
                            if len(failed_examples) < 10:
                                failed_examples.append(f"{file_path} -> 源文件不存在（复制前）")
                            continue

                        # ------------------ 原子复制流程（临时文件 -> fsync -> os.replace） ------------------
                        copy_verified = False
                        src_size_before = -1
                        try:
                            try:
                                src_size_before = file_path.stat().st_size
                            except Exception:
                                src_size_before = -1

                            dest_tmp = dest_file.parent / (dest_file.name + ".part")
                            if dest_tmp.exists():
                                try:
                                    dest_tmp.unlink()
                                    logger.info(f"移除残留临时文件：{repr(str(dest_tmp))}")
                                except Exception:
                                    dest_tmp = dest_file.parent / (dest_file.name + f".part-{int(time.time())}")
                                    logger.info(f"使用时间戳临时文件：{repr(str(dest_tmp))}")

                            # 优先尝试 SystemUtils.copy （并记录返回）
                            syscopy_used = False
                            sys_state = None
                            sys_err = None
                            try:
                                logger.info(f"调用 SystemUtils.copy: {repr(str(file_path))} -> {repr(str(dest_tmp))}")
                                state, error = SystemUtils.copy(file_path, dest_tmp)
                                syscopy_used = True
                                sys_state = state
                                sys_err = error
                                logger.info(f"SystemUtils.copy 返回: state={state}, error={error}")
                            except Exception as e_sys:
                                syscopy_used = False
                                sys_err = str(e_sys)
                                logger.warning(f"SystemUtils.copy 调用异常: {e_sys}")

                            write_ok = False
                            if syscopy_used and sys_state == 0:
                                # 如果 SystemUtils.copy 返回成功，尽力 fsync
                                try:
                                    with open(str(dest_tmp), "rb") as ftmp:
                                        try:
                                            os.fsync(ftmp.fileno())
                                        except Exception as efs:
                                            logger.debug(f"fsync 临时文件失败（可忽略）：{efs}")
                                except Exception:
                                    logger.debug("无法打开 SystemUtils.copy 生成的临时文件以 fsync")
                                write_ok = True
                                logger.info("SystemUtils.copy 写入临时文件成功")
                            else:
                                # 回退到 shutil.copy2
                                try:
                                    logger.info(f"回退到 shutil.copy2: {repr(str(file_path))} -> {repr(str(dest_tmp))} (info: {sys_err})")
                                    shutil.copy2(str(file_path), str(dest_tmp))
                                    try:
                                        with open(str(dest_tmp), "rb") as ftmp:
                                            try:
                                                os.fsync(ftmp.fileno())
                                            except Exception as efs:
                                                logger.debug(f"fsync 临时文件失败（可忽略）：{efs}")
                                    except Exception:
                                        logger.debug("无法打开回退的临时文件以 fsync")
                                    write_ok = True
                                    logger.info("shutil.copy2 写入临时文件成功")
                                except Exception as e_shutil:
                                    write_ok = False
                                    logger.error(f"shutil.copy2 复制失败：{e_shutil}")

                            if write_ok:
                                try:
                                    os.replace(str(dest_tmp), str(dest_file))
                                    logger.info(f"os.replace 原子替换成功：{repr(str(dest_file))}")
                                except Exception as erepl:
                                    logger.error(f"os.replace 原子替换失败：{dest_tmp} -> {dest_file} ({erepl})")
                                    try:
                                        if dest_tmp.exists():
                                            dest_tmp.unlink()
                                    except Exception:
                                        pass

                            # 最终验证：目标存在且大小与源一致（优先使用 src_size_before）
                            try:
                                if dest_file.exists():
                                    try:
                                        dst_size = dest_file.stat().st_size
                                    except Exception:
                                        dst_size = -1
                                    if src_size_before > 0:
                                        if dst_size == src_size_before:
                                            copy_verified = True
                                            logger.info(f"最终验证通过：src_size={src_size_before}, dst_size={dst_size}")
                                        else:
                                            logger.error(f"最终验证失败：src_size={src_size_before}, dst_size={dst_size} ({file_path} -> {dest_file})")
                                    else:
                                        if dst_size > 0:
                                            copy_verified = True
                                            logger.info(f"最终验证通过（无 src_size）: dst_size={dst_size}")
                                        else:
                                            logger.error(f"目标文件大小为0，视为复制失败：{dest_file}")
                                else:
                                    logger.error(f"目标文件不存在，复制失败：{dest_file}")
                            except Exception as everify:
                                logger.error(f"验证复制时异常：{everify}")
                                copy_verified = False

                            # 若复制失败清理残留临时文件
                            if not copy_verified:
                                try:
                                    if dest_tmp.exists():
                                        dest_tmp.unlink()
                                        logger.info(f"清理残留临时文件：{repr(str(dest_tmp))}")
                                except Exception:
                                    pass

                        except Exception as e_outer:
                            logger.error(f"复制过程中异常：{e_outer}")
                            try:
                                if 'dest_tmp' in locals() and dest_tmp.exists():
                                    dest_tmp.unlink()
                            except Exception:
                                pass
                            copy_verified = False

                        # 严格删除前检查（确保安全）：大小匹配 + 非同一 inode + 目标 mtime >= 源 mtime
                        if copy_verified and self._delete_source:
                            try:
                                if not dest_file.exists():
                                    logger.error(f"目标在删除前已不存在，跳过删除：{dest_file}")
                                else:
                                    try:
                                        dst_stat = dest_file.stat()
                                    except Exception:
                                        dst_stat = None
                                    try:
                                        src_stat = file_path.stat()
                                    except Exception:
                                        src_stat = None

                                    safe_to_delete = False
                                    if src_stat and dst_stat:
                                        if src_stat.st_size == dst_stat.st_size:
                                            same_file = False
                                            try:
                                                same_file = (hasattr(src_stat, "st_ino") and hasattr(dst_stat, "st_ino") and
                                                             src_stat.st_ino == dst_stat.st_ino and src_stat.st_dev == dst_stat.st_dev)
                                            except Exception:
                                                same_file = False
                                            if same_file:
                                                logger.warning(f"源和目标为同一底层文件，跳过删除：{file_path} -> {dest_file}")
                                                safe_to_delete = False
                                            else:
                                                try:
                                                    if dst_stat.st_mtime >= src_stat.st_mtime:
                                                        safe_to_delete = True
                                                    else:
                                                        logger.warning(f"目标文件时间比源早，跳过删除：dst_mtime={dst_stat.st_mtime}, src_mtime={src_stat.st_mtime}")
                                                        safe_to_delete = False
                                                except Exception:
                                                    safe_to_delete = True
                                        else:
                                            logger.error(f"目标大小与源不一致，跳过删除：src={src_stat.st_size if src_stat else 'N/A'}, dst={dst_stat.st_size if dst_stat else 'N/A'}")
                                            safe_to_delete = False
                                    else:
                                        logger.warning("无法获取 src/dst stat，跳过删除以保证安全")
                                        safe_to_delete = False

                                    if safe_to_delete:
                                        if file_path.exists():
                                            try:
                                                file_path.unlink()
                                                item_deleted += 1
                                                total_deleted += 1
                                                if len(deleted_examples) < 10:
                                                    deleted_examples.append(str(file_path))
                                                logger.info(f"已删除源文件（确认复制成功后）：{repr(str(file_path))}")
                                            except Exception as edelete:
                                                logger.error(f"删除源文件失败：{file_path} -> {edelete}")
                                                if len(failed_examples) < 10:
                                                    failed_examples.append(f"{file_path} -> 删除失败: {edelete}")
                                        else:
                                            logger.warning(f"欲删除的源文件已不存在（跳过删除）：{repr(str(file_path))}")
                                    else:
                                        logger.warning(f"删除条件不满足，跳过删除：{file_path} -> {dest_file}")
                            except Exception as e_check_del:
                                logger.error(f"删除前检查异常：{e_check_del}")

                        # 统计复制成功/失败
                        if copy_verified:
                            item_copied += 1
                            total_copied += 1
                            if len(copied_examples) < 10:
                                copied_examples.append(str(dest_file))
                        else:
                            item_failed += 1
                            total_failed += 1
                            if len(failed_examples) < 10:
                                failed_examples.append(f"{file_path} -> 复制失败")

                    except Exception as ex:
                        logger.error(f"处理文件 {repr(str(file_obj))} 时异常：{ex}")
                        item_failed += 1
                        total_failed += 1
                        if len(failed_examples) < 10:
                            failed_examples.append(f"{file_obj} -> 异常: {ex}")

                    # 延时逻辑（批量延时）
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

                # 单个监控项结束日志（详细统计）
                logger.info(f"==== 结束监控项: {repr(str(src_base))} -> {repr(str(tgt_base))} ====")
                logger.info(f"本项统计: 发现={item_found}, 成功={item_copied}, 跳过={item_skipped}, 失败={item_failed}, 删除={item_deleted}")

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
                    self.post_message(title=title, mtype=NotificationType.SiteMessage, text=message)
                except Exception as e:
                    logger.error(f"发送通知失败：{e}")

            logger.info(f"全量复制监控目录完成！ 版本{self.plugin_version}")
        finally:
            lock.release()

    def __update_config(self):
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

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
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
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'delete', 'label': '复制成功后删除源文件'}}
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VTextField', 'props': {'model': 'cron', 'label': '定时全量同步周期', 'placeholder': '5位cron表达式，留空关闭'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VTextField', 'props': {'model': 'delay', 'label': '随机延时', 'placeholder': '20,1-10  处理10个文件后随机延迟1-10秒'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'preserve_dirs', 'label': '保留子目录结构到目标（开启：保留；关闭：只平铺文件）'}}
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {'component': 'VTextarea', 'props': {'model': 'monitor_dirs', 'label': '监控目录', 'rows': 5,
                                                                       'placeholder': '监控目录:转移目的目录，示例：\\n/pan2/下载/电影/:/pan2/快速上传115/电影/\\n/pan2/下载/电视剧/:/pan2/快速上传115/电视剧/'}}
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {'component': 'VTextarea', 'props': {'model': 'rmt_mediaext', 'label': '文件格式', 'rows': 2,
                                                                        'placeholder': ".mp4, .mkv, .avi, .ts, .srt, .ass, .ssa, .sub, .idx"}}
                                ]
                            }
                        ]
                    }
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
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                self._event.set()
                self._scheduler.shutdown()
                self._event.clear()
            self._scheduler = None
