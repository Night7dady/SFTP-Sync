#!/usr/bin/env python3
"""
SFTP -> OSS 流式同步脚本

每晚由 cron 启动,到 deadline 自动退出。
逐文件同步:SFTP 下载 -> OSS 上传 -> 删本地。
用 OSS 中已有的文件列表做去重,跑过的不再跑。
"""
import configparser
import logging
import os
import posixpath
import signal
import stat
import sys
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import oss2
import paramiko


CONFIG_PATH = "/etc/sftp-sync/config.ini"


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
def parse_deadline(deadline_str: str) -> datetime:
    """把 '07:30' 解析成今天 07:30 的 datetime。
    如果当前时间已经过了今天的 deadline,认为是次日的 deadline
    (支持 23:30 启动跨夜到次日 07:30 的场景)。"""
    h, m = map(int, deadline_str.split(":"))
    now = datetime.now()
    today_dl = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now > today_dl:
        today_dl = today_dl + timedelta(days=1)
    return today_dl


def reached_deadline(deadline: datetime) -> bool:
    return datetime.now() >= deadline


# ---------- SFTP ----------
def open_sftp(cfg) -> paramiko.SFTPClient:
    """建立 SFTP 连接,返回 SFTPClient。"""
    transport = paramiko.Transport((cfg["sftp"]["host"], int(cfg["sftp"]["port"])))
    transport.connect(username=cfg["sftp"]["user"],
                       password=cfg["sftp"]["password"])
    sftp = paramiko.SFTPClient.from_transport(transport)
    # 注意:transport 不能 close,否则 sftp 也会断
    sftp._transport_holder = transport  # 保留引用防 GC
    return sftp


def list_remote_files(sftp: paramiko.SFTPClient, remote_dir: str,
                       relative: str = "") -> list:
    """递归列出 remote_dir 下所有文件,返回 [(相对路径, 大小), ...]。

    relative 用于递归时记录相对于 remote_dir 的路径前缀。"""
    results = []
    full_dir = posixpath.join(remote_dir, relative) if relative else remote_dir
    for entry in sftp.listdir_attr(full_dir):
        if entry.filename.startswith("."):
            continue  # 跳过 .bash_history 这类隐藏文件
        rel_path = posixpath.join(relative, entry.filename) if relative else entry.filename
        if stat.S_ISDIR(entry.st_mode):
            # 递归进子目录
            results.extend(list_remote_files(sftp, remote_dir, rel_path))
        else:
            results.append((rel_path, entry.st_size))
    return results


# ---------- OSS ----------
def open_oss_bucket(cfg) -> oss2.Bucket:
    auth = oss2.Auth(cfg["oss"]["access_key_id"], cfg["oss"]["access_key_secret"])
    bucket = oss2.Bucket(auth, cfg["oss"]["endpoint"], cfg["oss"]["bucket"])
    return bucket


def list_oss_keys(bucket: oss2.Bucket, prefix: str) -> dict:
    """列出 OSS 中 prefix 下所有 object,返回 {key 去掉前缀: size}"""
    result = {}
    for obj in oss2.ObjectIteratorV2(bucket, prefix=prefix):
        rel = obj.key[len(prefix):] if obj.key.startswith(prefix) else obj.key
        result[rel] = obj.size
    return result


# ---------- 主同步逻辑 ----------
def sync_exchange(sftp, bucket, exchange: str, cfg, logger,
                   deadline: datetime) -> dict:
    """同步一个交易所目录,返回统计 dict。"""
    remote_root = cfg["sync"]["remote_root"]
    local_staging = Path(cfg["sync"]["local_staging"])
    remote_dir = posixpath.join(remote_root, exchange)
    oss_prefix = f"{exchange}/"

    stats = {"checked": 0, "uploaded": 0, "skipped": 0, "failed": 0,
             "bytes": 0}

    logger.info(f"[{exchange}] Listing remote files...")
    try:
        remote_files = list_remote_files(sftp, remote_dir)
    except IOError as e:
        logger.error(f"[{exchange}] Cannot list remote dir: {e}")
        return stats
    logger.info(f"[{exchange}] Remote files: {len(remote_files)}")

    logger.info(f"[{exchange}] Listing OSS objects...")
    oss_keys = list_oss_keys(bucket, oss_prefix)
    logger.info(f"[{exchange}] OSS objects: {len(oss_keys)}")

    # 算差集:远端有 + (OSS 没有 OR 大小不一致) → 需要同步
    todo = []
    for rel_path, size in remote_files:
        if rel_path in oss_keys and oss_keys[rel_path] == size:
            stats["skipped"] += 1
        else:
            todo.append((rel_path, size))
    logger.info(f"[{exchange}] To sync: {len(todo)}")

    # 逐文件处理
    for rel_path, size in todo:
        if reached_deadline(deadline):
            logger.warning(f"[{exchange}] Deadline reached, stopping.")
            break

        stats["checked"] += 1
        remote_path = posixpath.join(remote_dir, rel_path)
        local_path = local_staging / rel_path
        oss_key = f"{oss_prefix}{rel_path}"

        # 子目录下的文件需要先建本地父目录
        local_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            t0 = time.time()
            logger.info(f"[{exchange}] DL  {rel_path} ({size/1e6:.1f} MB)")
            sftp.get(remote_path, str(local_path))
            t1 = time.time()
            dl_speed = size / (t1 - t0) / 1e6 if t1 > t0 else 0

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
            stats["failed"] += 1
            logger.error(f"[{exchange}] FAIL {rel_path}: {e}")
        finally:
            # 不管成败,删本地文件释放磁盘
            if local_path.exists():
                local_path.unlink()

    return stats


def main():
    cfg = load_config()
    logger = setup_logger(cfg["sync"]["log_dir"])
    deadline = parse_deadline(cfg["sync"]["deadline"])

    logger.info("===== SFTP->OSS sync started =====")
    logger.info(f"Window ends at: {deadline:%Y-%m-%d %H:%M:%S}")

    # 准备 staging 目录
    Path(cfg["sync"]["local_staging"]).mkdir(parents=True, exist_ok=True)

    # 连接
    try:
        sftp = open_sftp(cfg)
        logger.info("SFTP connected.")
    except Exception as e:
        logger.error(f"SFTP connection failed: {e}")
        sys.exit(1)

    bucket = open_oss_bucket(cfg)
    logger.info(f"OSS bucket: {cfg['oss']['bucket']} @ {cfg['oss']['endpoint']}")

    exchanges = [x.strip() for x in cfg["sync"]["exchanges"].split(",") if x.strip()]

    grand_total = {"uploaded": 0, "skipped": 0, "failed": 0, "bytes": 0}
    for exchange in exchanges:
        if reached_deadline(deadline):
            logger.warning("Deadline reached before all exchanges done.")
            break
        s = sync_exchange(sftp, bucket, exchange, cfg, logger, deadline)
        for k in grand_total:
            grand_total[k] += s.get(k, 0)
        logger.info(f"[{exchange}] DONE  uploaded={s['uploaded']} "
                     f"skipped={s['skipped']} failed={s['failed']} "
                     f"bytes={s['bytes']/1e9:.2f} GB")

    sftp.close()

    logger.info(f"===== Sync finished. uploaded={grand_total['uploaded']} "
                 f"skipped={grand_total['skipped']} "
                 f"failed={grand_total['failed']} "
                 f"total={grand_total['bytes']/1e9:.2f} GB =====")


if __name__ == "__main__":
    main()
