"""通过 SSH 连接设备，并按需升级 hawaii_ii。"""

import argparse
import base64
from contextlib import closing
import hashlib
import os
from pathlib import Path
import re
import shlex
import sys

import paramiko


SSH_TIMEOUT = 15
STAGING_TEMPLATE = "/usr/bin/.hawaii-upgrade.XXXXXXXX"
KNOWN_HOSTS_DIR = Path.home() / ".flash_arm_app" / "known_hosts"

# 在设备端完成停机、替换和恢复，减少客户端断连对事务的影响。
UPGRADE_SCRIPT = """\
set -u
trap '' HUP
stage=$1
target=/usr/bin/hawaii_ii
backup="$stage/previous"
incoming="$stage/incoming"
preserve_stage=0

cleanup() {
    if [ "$preserve_stage" -eq 0 ]; then
        rm -f "$incoming" "$backup" && rmdir "$stage" ||
            echo "Could not clean up staging directory $stage." >&2
    fi
}

fail() {
    echo "$1" >&2
    exit 1
}

restart_old() {
    if systemctl start hawaii_app && systemctl is-active --quiet hawaii_app; then
        echo "The previous service is running."
    else
        echo "The previous executable is in place, but the service did not start." >&2
    fi
}

rollback() {
    if mv -fT "$backup" "$target"; then
        echo "Restored the previous executable."
        restart_old
    else
        preserve_stage=1
        echo "Could not restore the previous executable. Backup kept at $backup." >&2
    fi
    exit 1
}

trap cleanup EXIT
exec 9>/run/lock/hawaii_ii-upgrade.lock
flock -n 9 || fail "Could not acquire the upgrade lock. Check flock or wait for the other upgrade to finish."
[ -f "$target" ] && [ ! -L "$target" ] ||
    fail "Could not back up the previous executable. The target must be a regular file."
cp -p "$target" "$backup" || fail "Could not back up the previous executable."
chmod 0755 "$incoming" || fail "Could not make the new file executable."
systemctl stop hawaii_app || fail "Could not stop hawaii_app."
if ! mv -fT "$incoming" "$target"; then
    echo "Could not replace hawaii_ii." >&2
    # 如果旧程序仍在原位，直接重启，避免再次尝试可能被拒绝的替换。
    if [ -f "$target" ] && [ ! -L "$target" ] && cmp -s "$backup" "$target"; then
        echo "The previous executable is still in place."
        restart_old
        exit 1
    fi
    rollback
fi
if ! { systemctl start hawaii_app && systemctl is-active --quiet hawaii_app; }; then
    echo "Could not start hawaii_app with the new executable." >&2
    systemctl stop hawaii_app || echo "Could not stop hawaii_app before rollback." >&2
    rollback
fi
"""


class UpgradeError(Exception):
    """升级准备、远程操作或结果确认失败。"""


class ConfirmHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """核对首次连接的密钥，并记录已确认的设备。"""

    def __init__(self, key_path: Path):
        self._key_path = key_path

    def missing_host_key(
        self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey
    ) -> None:
        """在 SSH 登录前要求操作者核对指纹。"""
        digest = hashlib.sha256(key.asbytes()).digest()
        fingerprint = base64.b64encode(digest).decode("ascii").rstrip("=")
        print(f"SSH host key fingerprint for {hostname}: SHA256:{fingerprint}")
        if sys.stdin is None or not sys.stdin.isatty():
            raise paramiko.SSHException(
                "An interactive terminal is required to verify a new SSH host key."
            )
        try:
            answer = input(
                "Verify the fingerprint against a trusted source. "
                "Type yes to trust this host key: "
            )
        except EOFError:
            answer = ""
        if answer.strip().lower() != "yes":
            raise paramiko.SSHException("The SSH host key was not confirmed.")

        self._key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # 每个设备独立保存。并发首次确认时，不能覆盖先保存的密钥。
        try:
            with self._key_path.open("x", encoding="ascii") as key_file:
                key_file.write(f"{hostname} {key.get_name()} {key.get_base64()}\n")
        except FileExistsError:
            keys = paramiko.HostKeys(str(self._key_path))
            if not keys.check(hostname, key):
                raise paramiko.SSHException("The SSH host key does not match the saved key.")
        client.get_host_keys().add(hostname, key.get_name(), key)


class DeviceUpgrader:
    """通过已连接的 SSH 会话上传文件并触发升级。"""

    def __init__(self, client: paramiko.SSHClient):
        self._client = client

    def upgrade(self, local_file: Path) -> None:
        """在同一文件系统暂存，再执行远程服务切换。"""
        with local_file.open("rb") as source:
            size = os.fstat(source.fileno()).st_size
            if size == 0:
                raise UpgradeError(f"Upgrade file is empty: {local_file}")
            # 替换时使用同一文件系统的重命名，避免目标路径暴露半份文件。
            stage = self._run(f"mktemp -d {STAGING_TEMPLATE}").strip()
            if not re.fullmatch(r"/usr/bin/\.hawaii-upgrade\.[a-zA-Z0-9]{8}", stage):
                raise UpgradeError("The device did not return a valid staging directory.")
            try:
                with self._client.open_sftp() as sftp:
                    uploaded = sftp.putfo(source, f"{stage}/incoming", file_size=size)
                    if uploaded.st_size != size:
                        raise UpgradeError("The uploaded file size does not match the local file.")
            except (UpgradeError, OSError, paramiko.SSHException, EOFError) as error:
                self._cleanup_upload(stage)
                raise UpgradeError(f"Could not upload the upgrade file: {error}") from error

        command = shlex.join(("sh", "-c", UPGRADE_SCRIPT, "upgrade", stage))
        try:
            output = self._run(command)
        except (OSError, paramiko.SSHException, EOFError) as error:
            raise UpgradeError(
                "Could not confirm the upgrade result. "
                f"Check hawaii_app and staging directory {stage}: {error}"
            ) from error
        if output.strip():
            print(output.strip())

    def _cleanup_upload(self, stage: str) -> None:
        """上传中断时清理本次暂存文件。"""
        incoming = shlex.quote(f"{stage}/incoming")
        try:
            self._run(f"rm -f {incoming} && rmdir {shlex.quote(stage)}")
        except (UpgradeError, OSError, paramiko.SSHException, EOFError) as error:
            print(f"Could not clean up staging directory {stage}: {error}", file=sys.stderr)

    def _run(self, command: str) -> str:
        """合并读取远程输出，并检查命令退出状态。"""
        transport = self._client.get_transport()
        if transport is None:
            raise paramiko.SSHException("The SSH session is unavailable.")
        with closing(transport.open_session(timeout=SSH_TIMEOUT)) as channel:
            channel.set_combine_stderr(True)
            channel.exec_command(command)
            channel.shutdown_write()
            with channel.makefile("rb") as stream:
                output = stream.read().decode("utf-8", errors="replace")
            status = channel.recv_exit_status()
        if status == -1:
            raise paramiko.SSHException("The device did not return a command exit status.")
        if status != 0:
            raise UpgradeError(output.strip() or f"Remote command exited with status {status}.")
        return output


def parse_args() -> argparse.Namespace:
    """读取命令行参数。"""
    parser = argparse.ArgumentParser(
        description="Connect to a device over SSH and optionally upgrade hawaii_ii."
    )
    parser.add_argument("-d", default="192.168.1.10", help="Device IP (default: %(default)s)")
    parser.add_argument("-u", default="root", help="SSH username (default: %(default)s)")
    parser.add_argument("-p", default="q", help="SSH password (default: %(default)s)")
    parser.add_argument("-f", type=Path, help="Local upgrade file. Omit to connect only.")
    return parser.parse_args()


def connect(client: paramiko.SSHClient, args: argparse.Namespace) -> None:
    """加载主机密钥后，使用命令行账号和密码登录设备。"""
    # 用地址的散列作文件名，避免把用户输入当作本地路径。
    key_path = KNOWN_HOSTS_DIR / hashlib.sha256(args.d.encode()).hexdigest()
    if key_path.exists():
        # 已确认的设备以本工具保存的密钥为准，错误或空记录也不能重新信任。
        client.load_host_keys(str(key_path))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        client.load_system_host_keys()
        client.set_missing_host_key_policy(ConfirmHostKeyPolicy(key_path))
    client.connect(
        args.d,
        username=args.u,
        password=args.p,
        timeout=SSH_TIMEOUT,
        banner_timeout=SSH_TIMEOUT,
        auth_timeout=SSH_TIMEOUT,
        allow_agent=False,
        look_for_keys=False,
    )


def validate_upgrade_file(path: Path) -> None:
    """在连接前拒绝缺失、空或不可读的普通文件。"""
    if not path.is_file():
        raise UpgradeError(f"Upgrade file does not exist or is not a regular file: {path}")
    with path.open("rb") as source:
        if not source.read(1):
            raise UpgradeError(f"Upgrade file is empty: {path}")


def main() -> int:
    """连接设备并返回命令行退出状态。"""
    args = parse_args()
    try:
        if args.f is not None:
            validate_upgrade_file(args.f)
        with closing(paramiko.SSHClient()) as client:
            connect(client, args)
            print(f"Connected to {args.d} over SSH.")
            if args.f is not None:
                DeviceUpgrader(client).upgrade(args.f)
                print("Device upgrade complete.")
    except (UpgradeError, OSError, paramiko.SSHException, EOFError) as error:
        print(f"Operation failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nOperation interrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
