# SFTP → OSS 行情数据同步系统

每晚自动把朋友 SFTP 服务器上的行情数据(8 个交易所)同步到阿里云 OSS bucket `marketdata-cn`。

---

## 1. 整体架构

```
SFTP 服务器 (120.41.7.126:65022, /data/)
     │
     │ 1. paramiko SFTP 下载
     ▼
ECS 临时目录 (/data/staging/)
     │
     │ 2. oss2 SDK 上传(走内网,免费)
     ▼
OSS Bucket (marketdata-cn, oss-cn-shanghai)
     │
     └── 3. 上传成功后立即删本地文件,释放磁盘
```

**特点**

- 流式管道:单文件最大占用 ~5 GB 临时磁盘,不会撑爆 35 G 盘
- 去重:用 OSS 已有对象列表做差集,跑过的不重复跑
- 优雅退出:到 deadline 时间自动停止,不会越界占用业务时段
- 启动前自动清空 staging,防止残留累积
- 幂等:可以随时手动重跑,已传的会跳过

---

## 2. 文件路径速查

| 用途 | 路径 |
|---|---|
| 主脚本 | `/opt/sftp-sync/sync.py` |
| README 本身 | `/opt/sftp-sync/README.md` |
| 配置文件(SFTP 凭据 + OSS AK + 参数) | `/etc/sftp-sync/config.ini` |
| 日志目录 | `/var/log/sftp-sync/` |
| 每日同步日志 | `/var/log/sftp-sync/sync-YYYYMMDD.log` |
| cron 标准输出/错误日志 | `/var/log/sftp-sync/cron.log` |
| 临时下载区(自动清理) | `/data/staging/` |
| ossutil 配置 | `/root/.ossutilconfig` |

> ⚠️ `/etc/sftp-sync/config.ini` 权限为 600(只有 root 能读),内含 SFTP 密码和 OSS AccessKey。**严禁外传或贴到聊天里**。

---

## 3. 配置文件 (`/etc/sftp-sync/config.ini`)

```ini
[sftp]
host = <SFTP地址>
port = <SFTP端口>
user = <SFTP用户名>
password = <SFTP 密码>

[oss]
bucket = <bucket 名称>
endpoint = <oss 地址>
access_key_id = <RAM 子账号 AK ID>
access_key_secret = <RAM 子账号 AK Secret>

[sync]
remote_root = /data
local_staging = /data/staging
log_dir = /var/log/sftp-sync
exchanges = 
deadline = <结束时间 24H> eg: 7:30
```

**字段说明**

- `exchanges`:同步顺序,**靠前的优先**。当前优先 SSE/SZSE(大文件,带宽利用率高)
- `deadline`:每天到这个时间自动优雅退出(格式 `HH:MM`,24 小时制)
- `endpoint`:走 OSS 内网 endpoint,流量免费

---

## 4. 定时任务(cron)

```bash
# 查看
sudo crontab -l
```

**当前配置**

```
30 23 * * * rm -rf /data/staging/* 2>/dev/null; /opt/sftp-sync/sync.py >> /var/log/sftp-sync/cron.log 2>&1
```

含义:每天 **23:30** 启动,执行前先清空 staging 目录。

**修改时间**

```bash
sudo crontab -e
```

改完保存即生效,无需重启 cron 服务。

---

## 5. OSS 数据结构

```
oss://marketdata-cn/
├── CFFEX/  中金所:股指/国债期货
│   ├── 20221220.tar.gz
│   ├── ...
│   └── 20260512.tar
├── CZCE/   郑商所
├── DCE/    大商所(文件最多,含"目录形式"日期)
│   ├── 20231009_034001.tar.gz   早期格式带时间戳
│   ├── 20241029.tar             新格式
│   ├── 20260108/                偶有目录形式日期
│   │   └── xxxxx.dat
│   └── ...
├── GFEX/   广期所
├── INE/    上期能源
├── SHFE/   上期所
├── SSE/    上交所(.tar.zst 大文件 3-5 GB)
└── SZSE/   深交所(.tar.zst 大文件 3-7 GB)
```

---

## 6. 常用运维命令

### 6.1 看同步结果

```bash
# 看今天的同步总结(凌晨 0 点后用)
grep "DONE\|Sync finished" /var/log/sftp-sync/sync-$(date +%Y%m%d).log

# 看昨晚启动的同步总结(日志按启动日命名)
grep "DONE\|Sync finished" /var/log/sftp-sync/sync-$(date -d 'yesterday' +%Y%m%d).log

# 看实时进度(同步运行时)
tail -f /var/log/sftp-sync/sync-$(date +%Y%m%d).log
```

### 6.2 看 OSS 上每个交易所的文件数

```bash
for ex in CFFEX GFEX CZCE INE SHFE DCE SZSE SSE; do
  count=$(ossutil ls oss://marketdata-cn/$ex/ -e oss-cn-shanghai-internal.aliyuncs.com 2>/dev/null | grep "Object Number" | awk '{print $NF}')
  echo "$ex: $count files"
done
```

### 6.3 检查失败

```bash
grep -i "FAIL\|ERROR" /var/log/sftp-sync/sync-$(date +%Y%m%d).log
```

### 6.4 看磁盘占用

```bash
df -h /
du -sh /data/staging/
```

### 6.5 手动启动一次

```bash
sudo /opt/sftp-sync/sync.py
```

脚本是**幂等的**——已传的文件会 skip,可以放心反复跑。

### 6.6 临时调整 deadline(测试用)

```bash
# 改成 5 分钟后
NOW=$(date -d "+5 minutes" +%H:%M)
sudo sed -i "s/^deadline = .*/deadline = $NOW/" /etc/sftp-sync/config.ini

# 改回 07:30(正式)
sudo sed -i "s/^deadline = .*/deadline = 07:30/" /etc/sftp-sync/config.ini
```

### 6.7 调整交易所同步顺序

```bash
sudo sed -i "s/^exchanges = .*/exchanges = SSE,SZSE,DCE,CFFEX,GFEX,CZCE,INE,SHFE/" /etc/sftp-sync/config.ini
```

### 6.8 看实时下载速度(运行时)

```bash
# 装一次
sudo apt install -y nload

# 看实时,按 q 退出
nload eth0
```

看 Incoming 的 Avg 值:

- 85+ Mbps:带宽吃满,升级有效
- 50-80 Mbps:对方有限制
- < 50 Mbps:对方限速,升带宽没用

---

## 7. 故障排查

### 7.1 脚本没自动跑

```bash
# 看 cron 服务在不在
systemctl status cron

# 看 cron 任务是否存在
sudo crontab -l

# 看 cron 是不是真的触发了(系统日志)
grep CRON /var/log/syslog | tail -20

# 看 cron 标准输出/错误
tail -50 /var/log/sftp-sync/cron.log
```

### 7.2 SFTP 连接失败

可能原因:朋友服务器宕机/重启、host key 变了、密码被修改、网络问题。

```bash
# 手动测试连接
lftp -p 65022 sftp://user005@120.41.7.126

# 如果是 host key 变了,清掉旧的
ssh-keygen -R '[120.41.7.126]:65022'

# 然后重新连一次接受新 key
ssh -p 65022 user005@120.41.7.126
```

### 7.3 OSS 上传失败

可能原因:AccessKey 被禁用或删除、bucket 权限策略变了、网络问题。

```bash
# 手动测试 OSS 访问
ossutil ls oss://marketdata-cn/ -e oss-cn-shanghai-internal.aliyuncs.com

# 看 AK 配置
sudo cat /root/.ossutilconfig
```

### 7.4 磁盘满了

```bash
# 看哪里满了
df -h /
du -sh /data/staging/
du -sh /var/log/sftp-sync/

# 清理 staging
sudo rm -rf /data/staging/*

# 清理旧日志(保留最近 7 天)
find /var/log/sftp-sync/ -name "sync-*.log" -mtime +7 -delete
```

### 7.5 某个文件死活传不上去

可以单独跳过这个文件,手动上传同名空文件占位:

```bash
echo "skip" | ossutil cp - oss://marketdata-cn/<交易所>/<文件名> -e oss-cn-shanghai-internal.aliyuncs.com
```

(只是应急方案,不推荐常用。正常应该看日志查具体报错)

---

## 8. 阿里云资源信息

| 资源 | 值 |
|---|---|
| ECS 实例 ID | `iZuf65iclmdqtv07ss9r9uZ` |
| ECS 地域 | 华东2(上海)`cn-shanghai` |
| OSS Bucket | `marketdata-cn` |
| OSS Region | `cn-shanghai` |
| 内网 Endpoint | `oss-cn-shanghai-internal.aliyuncs.com` |
| CNAME 域名 | `marketdata-cn.cn-shanghai.taihangcda.cn` |
| 当前带宽 | 100 Mbps(可临时升 200M 提速历史同步) |

---

## 9. 后续待办 / 优化方向

- [ ] **历史回填完成后**(预计 10-14 天),可以把 `exchanges` 顺序调回正常(顺序无关紧要)
- [ ] **加日志轮转**:可加 `find /var/log/sftp-sync/ -name "sync-*.log" -mtime +30 -delete` 到 cron
- [ ] **加告警**:失败数超过阈值时发钉钉/邮件(目前是失败 0,先不急)
- [ ] **加并发**(可选):当前单连接 SFTP 是瓶颈,4 路并发对小文件场景特别有效
- [ ] **AK / 密码轮换**:每 3-6 个月轮换一次 RAM AK 和 SFTP 密码

---

## 10. 联系方式 / 备忘

- SFTP 服务器属于朋友,出问题先问朋友确认服务器状态
- OSS 账单在阿里云控制台 → 费用中心 查看
- 临时升级带宽路径:阿里云控制台 → ECS 实例 → 更多 → 资源变更 → 修改带宽配置

---

## 附录 A:主脚本核心逻辑

`/opt/sftp-sync/sync.py` 的核心流程:

```
1. 读 /etc/sftp-sync/config.ini 取凭据和参数
2. 算 deadline(支持跨夜,如 23:30 启动到次日 07:30)
3. 连接 SFTP(paramiko)和 OSS(oss2)
4. 准备 staging 目录
5. 按 exchanges 顺序遍历每个交易所:
   a. 递归扫描 SFTP 远端目录(支持子目录)→ 文件列表
   b. 列出 OSS 已有对象 → key 集合
   c. 算差集得到待同步列表(对比文件名+大小)
   d. 逐文件:
      - paramiko SFTP get 到 staging
      - oss2 put_object 上传到 OSS
      - 删除 staging 本地文件
      - 每文件结束检查 deadline,到点优雅退出
6. 统计日志输出后退出
```

依赖库:`paramiko`(SFTP)、`oss2`(阿里云 OSS SDK)

---

## 附录 B:首次部署清单(参考,已完成)

如果将来需要在新机器上重建,按以下步骤:

```bash
# 1. 装依赖
sudo apt update && sudo apt install -y python3 python3-pip unzip lftp nload
sudo pip3 install --break-system-packages paramiko oss2

# 2. 装 ossutil (v2)
curl -o ossutil.zip https://gosspublic.alicdn.com/ossutil/v2/2.2.1/ossutil-2.2.1-linux-amd64.zip
unzip ossutil.zip
sudo mv ossutil-2.2.1-linux-amd64/ossutil /usr/local/bin/
sudo chmod +x /usr/local/bin/ossutil

# 3. 配置 ossutil
ossutil config
# 填:Region=cn-shanghai,Endpoint=https://oss-cn-shanghai-internal.aliyuncs.com

# 4. 建目录
sudo mkdir -p /data/staging /var/log/sftp-sync /etc/sftp-sync /opt/sftp-sync

# 5. 写配置文件(见第 3 节模板)
sudo nano /etc/sftp-sync/config.ini
sudo chmod 600 /etc/sftp-sync/config.ini

# 6. 写主脚本(从备份恢复 sync.py)
sudo nano /opt/sftp-sync/sync.py
sudo chmod +x /opt/sftp-sync/sync.py

# 7. 把 SFTP 服务器 host key 加入信任
ssh-keyscan -p 65022 120.41.7.126 >> ~/.ssh/known_hosts

# 8. 加 cron
sudo crontab -e
# 加:30 23 * * * rm -rf /data/staging/* 2>/dev/null; /opt/sftp-sync/sync.py >> /var/log/sftp-sync/cron.log 2>&1

# 9. 验证
python3 -c "import paramiko, oss2; print('OK')"
ossutil ls oss://marketdata-cn/ -e oss-cn-shanghai-internal.aliyuncs.com
sudo /opt/sftp-sync/sync.py    # 跑一次试试
```

---

*最后更新:2026-05-13*
