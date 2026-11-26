# ssh远程连接设备并控制设备

使用python, 当前虚拟环境为.venv

## 使用paramiko连接远程设备

脚本输入参数:
-d 设备ip 默认192.168.1.10
-u 登陆用户名 默认root
-p 登陆密码 默认q
-f 需要升级的文件 没有传则不升级 (替换设备根目录/usr/bin/hawaii_ii)
-h help

## 升级流程(hawaii_ii的执行是由systemd管理的):

1. 先stop: systemctl stop hawaii_app
2. 替换/usr/bin/hawaii_ii
3. start hawaii_ii: systemctl start hawaii_app

## 要求

1. 需要处理好第一次ssh连接到设备的认证
2. 需要使用面向对象编程,符合现代编程规范(使用google的python代码规范)
3. 需要能打包成可执行的exe文件
4. 每个功能一个函数,一个函数功能不能太多
5. 写好中文注释

## 运行

使用 Python 3.10 或更新版本。在 Linux 的项目根目录运行以下命令。如果 `.venv` 已存在，可以跳过创建环境的步骤。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python flash_arm_app.py -h
.venv/bin/python flash_arm_app.py -d 192.168.1.10
.venv/bin/python flash_arm_app.py -d 192.168.1.10 -f ./hawaii_ii
```

首次连接未知设备时，从可信来源核对工具显示的 SHA-256 主机密钥指纹，通过后输入 `yes`。工具把确认的密钥保存在用户目录的 `.flash_arm_app/known_hosts/` 下，后续优先核对此记录。

升级使用 SSH 和 SFTP。登录账号需要能在 `/usr/bin` 暂存和安装文件、在 `/run/lock` 取得 `flock` 锁，以及通过 `systemctl` 管理 `hawaii_app`。目标 `/usr/bin/hawaii_ii` 不存在时，工具跳过备份并安装新程序。如果首次安装后服务启动失败，保留新程序以便排查。已有程序时，工具先备份，替换或启动失败后尝试恢复旧程序和服务。请提供适用于设备的 `hawaii_ii` 可执行文件。

## 打包 Windows EXE

在 Windows 的 PowerShell 中进入项目根目录并运行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt "pyinstaller>=6,<7"
.\.venv\Scripts\python.exe -m PyInstaller --onefile --name flash_arm_app flash_arm_app.py
.\dist\flash_arm_app.exe -h
```

打包生成 `dist\flash_arm_app.exe`，可从终端运行。
