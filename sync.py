#!/usr/bin/env python3
"""
SFTP -> OSS 流式同步脚本 v3.1

相对 v3 的改进:
- 支持"周五启动后跨周末到下周一 07:30"模式
- 周一到周四:启动后跑到次日 07:30(8 小时)
- 周五/周六/周日启动:跑到下周一 07:30(最长 56 小时)
- 其他逻辑跟 v3 一样(lftp 后端下载 / 黑名单 / 流式管道)

cron 应该只在工作日启动:
  30 23 * * 1-5 ...
否则周六周日的 cron 会破坏周五任务正在下载的半成品。
"""
import configparser
import logging
import os
import posixpath
import stat
import subprocess
import sys
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import oss2
import paramiko


CONFIG_PATH = "/etc/sftp-sync/config.ini"
BLACKLIST_PATH = "/var/log/sftp-sync/blacklist.txt"
LFTP_TIMEOUT = 1800  # 单文件最长下载时间 30 分钟


# ---------- 配置 ----------
def load_config():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    return cfg


# ---------- 日志 ----------
def setup_logger(log_dir: str) -> logging.Logger:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_file = Path(log_dir) / f"sync-{datetime.now():%Y%m%d}.log"

    logger = logging.getLogger("sync")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s",
                             datefmt="%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(log_file)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


# ---------- 时间窗口 ----------
def parse_deadline(deadline_str: str, logger=None) -> datetime:
    """计算本次同步的 deadline。

    规则:
    - 周一(0) / 周二(1) / 周三(2) / 周四(3) 启动:跑到次日 07:30
    - 周五(4) 启动:跑到下周一 07:30(56 小时,跨完整周末)
    - 周六(5) / 周日(6) 启动:跑到下周一 07:30
      (理论上 cron 不会在周末启动,这是手动启动时的保护)

    deadline_str 格式 'HH:MM',目前固定 07:30,但保留可配置。
    """
    h, m = map(int, deadline_str.split(":"))
    now = datetime.now()
    weekday = now.weekday()  # 0=周一, 4=周五, 5=周六, 6=周日

    if weekday <= 3:
        # 周一到周四:次日 07:30
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now > target:
            target = target + timedelta(days=1)
        days_label = "next day"
    elif weekday == 4:
        # 周五:跨周末到下周一 07:30
        days_to_monday = 7 - weekday  # 4 -> 3 (周五+3=周一)
        target = now.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=days_to_monday)
        days_label = f"next Monday (cross weekend, ~{(target - now).total_seconds() / 3600:.0f}h)"
    else:
        # 周六/周日:也跑到下周一 07:30(手动启动的兜底)
        days_to_monday = 7 - weekday if weekday > 0 else 1
        target = now.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=days_to_monday)
        # 如果已经过了下周一 07:30 也不太可能,但兜底
        if now > target:
            target = target + timedelta(days=7)
        days_label = f"next Monday (~{(target - now).total_seconds() / 3600:.0f}h)"

    if logger:
        weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        logger.info(f"Today is {weekday_names[weekday]}, deadline scheme: run until {days_label}")

    return target


def reached_deadline(deadline: datetime) -> bool:
    return datetime.now() >= deadline


# ---------- 黑名单 ----------
def load_blacklist() -> set:
    blacklist = set()
    if Path(BLACKLIST_PATH).exists():
        with open(BLACKLIST_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    blacklist.add(line)
    return blacklist


def add_to_blacklist(entry: str):
    Path(BLACKLIST_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(BLACKLIST_PATH, "a") as f:
        f.write(f"{entry}\n")


# ---------- SFTP 列目录(用 paramiko) ----------
def open_sftp(cfg) -> paramiko.SFTPClient:
    transport = paramiko.Transport((cfg["sftp"]["host"], int(cfg["sftp"]["port"])))
    transport.set_keepalive(30)
    transport.connect(username=cfg["sftp"]["user"],
                       password=cfg["sftp"]["password"])
    sftp = paramiko.SFTPClient.from_transport(transport)
    sftp._transport_holder = transport
    return sftp


def list_remote_files(sftp: paramiko.SFTPClient, remote_dir: str,
                       relative: str = "") -> list:
    results = []
    full_dir = posixpath.join(remote_dir, relative) if relative else remote_dir
    for entry in sftp.listdir_attr(full_dir):
        if entry.filename.startswith("."):
            continue
        rel_path = (posixpath.join(relative, entry.filename)
                    if relative else entry.filename)
        if stat.S_ISDIR(entry.st_mode):
            try:
                results.extend(list_remote_files(sftp, remote_dir, rel_path))
            except IOError:
                pass
        else:
            results.append((rel_path, entry.st_size))
    return results


# ---------- lftp 下载 ----------
def lftp_download(cfg, remote_path: str, local_path: Path,
                   logger) -> tuple:
    host = cfg["sftp"]["host"]
    port = cfg["sftp"]["port"]
    user = cfg["sftp"]["user"]
    password = cfg["sftp"]["password"]

    local_path.parent.mkdir(parents=True, exist_ok=True)
    if local_path.exists():
        local_path.unlink()

    lftp_script = (
        f"set sftp:auto-confirm yes; "
        f"set net:max-retries 2; "
        f"set net:timeout 30; "
        f"set xfer:clobber on; "
        f"get '{remote_path}' -o '{str(local_path)}'; "
        f"bye"
    )

    cmd = [
        "lftp",
        "-p", str(port),
        "-u", f"{user},{password}",
        f"sftp://{host}",
        "-e", lftp_script,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=LFTP_TIMEOUT,
        )
        if result.returncode == 0 and local_path.exists() and local_path.stat().st_size > 0:
            return (True, "")

        err_combined = (result.stderr + result.stdout).lower()
        if "permission denied" in err_combined:
            return (False, "permission_denied")
        if "no such file" in err_combined:
            return (False, "no_such_file")
        err_msg = (result.stderr or result.stdout or "unknown error")[:300]
        return (False, err_msg.strip())

    except subprocess.TimeoutExpired:
        if local_path.exists():
            try:
                local_path.unlink()
            except Exception:
                pass
        return (False, "timeout")
    except Exception as e:
        return (False, f"exception: {e}")


# ---------- OSS ----------
def open_oss_bucket(cfg) -> oss2.Bucket:
    auth = oss2.Auth(cfg["oss"]["access_key_id"], cfg["oss"]["access_key_secret"])
    bucket = oss2.Bucket(auth, cfg["oss"]["endpoint"], cfg["oss"]["bucket"])
    return bucket


def list_oss_keys(bucket: oss2.Bucket, prefix: str) -> dict:
    result = {}
    for obj in oss2.ObjectIteratorV2(bucket, prefix=prefix):
        rel = obj.key[len(prefix):] if obj.key.startswith(prefix) else obj.key
        result[rel] = obj.size
    return result


# ---------- 主同步逻辑 ----------
def sync_exchange(sftp: paramiko.SFTPClient, bucket: oss2.Bucket,
                   exchange: str, cfg, logger,
                   deadline: datetime, blacklist: set) -> dict:
    remote_root = cfg["sync"]["remote_root"]
    local_staging = Path(cfg["sync"]["local_staging"])
    remote_dir = posixpath.join(remote_root, exchange)
    oss_prefix = f"{exchange}/"

    stats = {"checked": 0, "uploaded": 0, "skipped": 0,
             "blacklisted": 0, "failed": 0, "bytes": 0}

    logger.info(f"[{exchange}] Listing remote files...")
    try:
        remote_files = list_remote_files(sftp, remote_dir)
    except Exception as e:
        logger.error(f"[{exchange}] Cannot list remote dir: {e}")
        return stats
    logger.info(f"[{exchange}] Remote files: {len(remote_files)}")

    logger.info(f"[{exchange}] Listing OSS objects...")
    oss_keys = list_oss_keys(bucket, oss_prefix)
    logger.info(f"[{exchange}] OSS objects: {len(oss_keys)}")

    todo = []
    for rel_path, size in remote_files:
        bl_key = f"{exchange}/{rel_path}"
        if bl_key in blacklist:
            stats["blacklisted"] += 1
            continue
        if rel_path in oss_keys and oss_keys[rel_path] == size:
            stats["skipped"] += 1
        else:
            todo.append((rel_path, size))
    logger.info(f"[{exchange}] To sync: {len(todo)} "
                 f"(skipped={stats['skipped']}, blacklisted={stats['blacklisted']})")

    for rel_path, size in todo:
        if reached_deadline(deadline):
            logger.warning(f"[{exchange}] Deadline reached, stopping.")
            break

        stats["checked"] += 1
        remote_path = posixpath.join(remote_dir, rel_path)
        local_path = local_staging / rel_path
        oss_key = f"{oss_prefix}{rel_path}"

        t0 = time.time()
        logger.info(f"[{exchange}] DL  {rel_path} ({size/1e6:.1f} MB)")
        ok, err = lftp_download(cfg, remote_path, local_path, logger)
        t1 = time.time()

        if not ok:
            if err == "permission_denied":
                bl_key = f"{exchange}/{rel_path}"
                add_to_blacklist(bl_key)
                blacklist.add(bl_key)
                logger.warning(f"[{exchange}] BLACKLIST {rel_path} (permission denied, added to blacklist)")
                stats["blacklisted"] += 1
            else:
                logger.error(f"[{exchange}] FAIL DL {rel_path}: {err}")
                stats["failed"] += 1
            if local_path.exists():
                try:
                    local_path.unlink()
                except Exception:
                    pass
            continue

        dl_speed = size / (t1 - t0) / 1e6 if t1 > t0 else 0

        try:
            logger.info(f"[{exchange}] UP  {rel_path} -> oss")
            with open(local_path, "rb") as f:
                bucket.put_object(oss_key, f)
            t2 = time.time()
            up_speed = size / (t2 - t1) / 1e6 if t2 > t1 else 0
            stats["uploaded"] += 1
            stats["bytes"] += size
            logger.info(f"[{exchange}] OK  {rel_path} "
                         f"(dl {dl_speed:.1f}MB/s, up {up_speed:.1f}MB/s)")
        except Exception as e:
            logger.error(f"[{exchange}] FAIL UP {rel_path}: {e}")
            stats["failed"] += 1
        finally:
            if local_path.exists():
                try:
                    local_path.unlink()
                except Exception:
                    pass

    return stats


def main():
    cfg = load_config()
    logger = setup_logger(cfg["sync"]["log_dir"])
    deadline = parse_deadline(cfg["sync"]["deadline"], logger)

    logger.info("===== SFTP->OSS sync started (v3.1, weekday-aware) =====")
    logger.info(f"paramiko version: {paramiko.__version__}")
    logger.info(f"Window ends at: {deadline:%Y-%m-%d %H:%M:%S} "
                 f"({(deadline - datetime.now()).total_seconds() / 3600:.1f}h from now)")

    blacklist = load_blacklist()
    if blacklist:
        logger.info(f"Loaded {len(blacklist)} entries from blacklist.")

    Path(cfg["sync"]["local_staging"]).mkdir(parents=True, exist_ok=True)

    try:
        sftp = open_sftp(cfg)
        logger.info("SFTP connected (for listing).")
    except Exception as e:
        logger.error(f"SFTP connection failed: {e}")
        sys.exit(1)

    bucket = open_oss_bucket(cfg)
    logger.info(f"OSS bucket: {cfg['oss']['bucket']} @ {cfg['oss']['endpoint']}")

    exchanges = [x.strip() for x in cfg["sync"]["exchanges"].split(",") if x.strip()]

    grand_total = {"uploaded": 0, "skipped": 0, "blacklisted": 0,
                   "failed": 0, "bytes": 0}
    for exchange in exchanges:
        if reached_deadline(deadline):
            logger.warning("Deadline reached before all exchanges done.")
            break
        s = sync_exchange(sftp, bucket, exchange, cfg, logger,
                           deadline, blacklist)
        for k in grand_total:
            grand_total[k] += s.get(k, 0)
        logger.info(f"[{exchange}] DONE  uploaded={s['uploaded']} "
                     f"skipped={s['skipped']} blacklisted={s['blacklisted']} "
                     f"failed={s['failed']} bytes={s['bytes']/1e9:.2f} GB")

    try:
        sftp.close()
    except Exception:
        pass

    logger.info(f"===== Sync finished. uploaded={grand_total['uploaded']} "
                 f"skipped={grand_total['skipped']} "
                 f"blacklisted={grand_total['blacklisted']} "
                 f"failed={grand_total['failed']} "
                 f"total={grand_total['bytes']/1e9:.2f} GB =====")


if __name__ == "__main__":
    main()
