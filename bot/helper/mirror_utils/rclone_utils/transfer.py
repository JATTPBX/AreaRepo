from __future__ import annotations
from aiofiles import open as aiopen
from aiofiles.os import path as aiopath, listdir, makedirs
from asyncio import create_subprocess_exec, gather
from asyncio.subprocess import PIPE
from configparser import ConfigParser
from json import loads
from random import randrange
from re import findall as re_findall

from bot import config_dict, LOGGER
from bot.helper.ext_utils.bot_utils import cmd_exec, sync_to_async
from bot.helper.ext_utils.files_utils import get_mime_type, count_files_and_folders
from bot.helper.listeners import tasks_listener as task


class RcloneTransferHelper:
    def __init__(self, listener: task.TaskListener = None):
        self._listener = listener
        self._proc = None
        self._transferred_size = "0 B"
        self._eta = "~"
        self._percentage = "0%"
        self._speed = "0 B/s"
        self._size = "0 B"
        self._is_cancelled = False
        self._is_download = False
        self._is_upload = False
        self._sa_count = 1
        self._sa_index = 0
        self._sa_number = 0

    @property
    def transferred_size(self):
        return self._transferred_size

    @property
    def percentage(self):
        return self._percentage

    @property
    def speed(self):
        return self._speed

    @property
    def eta(self):
        return self._eta

    @property
    def size(self):
        return self._size

    async def _progress(self):
        while not (self._proc is None or self._is_cancelled):
            try:
                data = (await self._proc.stdout.readline()).decode()
            except:
                continue
            if not data:
                break
            if data := re_findall(
                r"Transferred:\s+([\d.]+\s*\w+)\s+/\s+([\d.]+\s*\w+),\s+([\d.]+%)\s*,\s+([\d.]+\s*\w+/s),\s+ETA\s+([\dwdhms]+)",
                data,
            ):
                (
                    self._transferred_size,
                    self._size,
                    self._percentage,
                    self._speed,
                    self._eta,
                ) = data[0]

    def _switchServiceAccount(self):
        if self._sa_index == self._sa_number - 1:
            self._sa_index = 0
        else:
            self._sa_index += 1
        self._sa_count += 1
        remote = f"sa{self._sa_index:03}"
        LOGGER.info("Switching to %s remote", remote)
        return remote

    @staticmethod
    async def _create_rc_sa(remote, remote_opts):
        sa_conf_dir = "rclone_sa"
        sa_conf_file = f"{sa_conf_dir}/{remote}.conf"
        if await aiopath.isfile(sa_conf_file):
            return sa_conf_file
        await makedirs(sa_conf_dir, exist_ok=True)
        if gd_id := remote_opts.get("team_drive"):
            option = "team_drive"
        elif gd_id := remote_opts.get("root_folder_id"):
            option = "root_folder_id"
        else:
            return "rclone.conf"
        files = await listdir("accounts")
        text = "".join(
            f"[sa{i:03}]\ntype = drive\nscope = drive\nservice_account_file = accounts/{sa}\n{option} = {gd_id}\n\n"
            for i, sa in enumerate(files)
        )
        async with aiopen(sa_conf_file, "w") as f:
            await f.write(text)
        return sa_conf_file

    async def _start_download(self, cmd, remote_type, using_sa):
        self._proc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        _, return_code = await gather(self._progress(), self._proc.wait())
        if self._is_cancelled:
            return
        if return_code == 0:
            await self._listener.onDownloadComplete()
        elif return_code != -9:
            error = (await self._proc.stderr.read()).decode().strip()
            if not error and using_sa:
                error = "Mostly your service accounts don't have acces to this drive!"
            LOGGER.error(error)
            if self._sa_number != 0 and "RATE_LIMIT_EXCEEDED" in error and using_sa:
                if self._sa_count < self._sa_number:
                    remote = self._switchServiceAccount()
                    cmd[6] = f"{remote}:{cmd[6].split(':', 1)[1]}"
                    if self._is_cancelled:
                        return
                    return await self._start_download(cmd, remote_type, using_sa)
                LOGGER.info(
                    "Reached maximum number of service accounts switching, which is %s",
                    self._sa_count,
                )
            if error:
                await self._listener.onDownloadError(error)
                return
            await self._listener.onDownloadComplete()

    async def download(self, remote, config_path, path):
        self._is_download = True
        try:
            remote_opts = await self._get_remote_options(config_path, remote)
        except Exception as err:
            await self._listener.onDownloadError(err)
            return
        remote_type = remote_opts["type"]
        using_sa = (
            remote_type == "drive"
            and config_dict["USE_SERVICE_ACCOUNTS"]
            and config_path == "rclone.conf"
            and await aiopath.isdir("accounts")
        )
        if using_sa:
            config_path = await self._create_rc_sa(remote, remote_opts)
            if config_path != "rclone.conf":
                sa_files = await listdir("accounts")
                self._sa_number = len(sa_files)
                self._sa_index = randrange(self._sa_number)
                remote = f"sa{self._sa_index:03}"
                LOGGER.info("Download with service account %s", remote)
        cmd = self._getUpdatedCommand(
            config_path, f"{remote}:{self._listener.link}", path, "copy"
        )
        if (
            remote_type == "drive"
            and not config_dict["RCLONE_FLAGS"]
            and not self._listener.rcFlags
        ):
            cmd.append("--drive-acknowledge-abuse")
        elif remote_type != "drive":
            cmd.extend(("--retries-sleep", "3s"))
        await self._start_download(cmd, remote_type, using_sa)

    async def _get_gdrive_link(self, config_path, remote, rc_path, mime_type):
        if mime_type == "Folder":
            epath = rc_path.strip("/").rsplit("/", 1)
            epath = f"{remote}:{epath[0]}" if len(epath) > 1 else f"{remote}:"
            destination = f"{remote}:{rc_path}"
        elif rc_path:
            epath = f"{remote}:{rc_path}/{self._listener.name}"
            destination = epath
        else:
            epath = f"{remote}:{rc_path}{self._listener.name}"
            destination = epath
        cmd = [
            "rclone",
            "lsjson",
            "--fast-list",
            "--no-mimetype",
            "--no-modtime",
            "--config",
            config_path,
            epath,
        ]
        res, err, code = await cmd_exec(cmd)
        if code == 0:
            result = loads(res)
            fid = next(
                (r["ID"] for r in result if r["Path"] == self._listener.name), "err"
            )
            link = (
                f"https://drive.google.com/drive/folders/{fid}"
                if mime_type == "Folder"
                else f"https://drive.google.com/uc?id={fid}&export=download"
            )
        elif code != -9:
            LOGGER.error(
                "While getting drive link. Path: %s. Stderr: %s", destination, err
            )
            link = ""
        return link, destination

    async def _start_upload(self, cmd, remote_type, using_sa):
        self._proc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        _, return_code = await gather(self._progress(), self._proc.wait())
        if self._is_cancelled:
            return False
        if return_code == -9:
            return False
        if return_code != 0:
            error = (await self._proc.stderr.read()).decode().strip()
            if not error and using_sa:
                error = "Mostly your service accounts don't have acces to this drive!"
            LOGGER.error(error)
            if self._sa_number != 0 and "RATE_LIMIT_EXCEEDED" in error and using_sa:
                if self._sa_count < self._sa_number:
                    remote = self._switchServiceAccount()
                    cmd[7] = f"{remote}:{cmd[7].split(':', 1)[1]}"
                    return (
                        False
                        if self._is_cancelled
                        else await self._start_upload(cmd, remote_type, using_sa)
                    )
                LOGGER.info(
                    "Reached maximum number of service accounts switching, which is %s",
                    self._sa_count,
                )
            if error:
                await self._listener.onUploadError(error)
                return False
        return True

    async def upload(self, path, size):
        self._is_upload = True
        rc_path = self._listener.upDest.strip("/")
        if rc_path.startswith("mrcc:"):
            rc_path = rc_path.split("mrcc:", 1)[1]
            oconfig_path = f"rclone/{self._listener.user_id}.conf"
        else:
            oconfig_path = "rclone.conf"
        oremote, rc_path = rc_path.split(":", 1)
        if await aiopath.isdir(path):
            mime_type = "Folder"
            folders, files = await count_files_and_folders(
                path, self._listener.extensionFilter
            )
            rc_path += f"/{self._listener.name}" if rc_path else self._listener.name
        else:
            if path.lower().endswith(tuple(self._listener.extensionFilter)):
                await self._listener.onUploadError(
                    f"This file extension is excluded by extension filter ({', '.join(self._listener.extensionFilter[2:])})!"
                )
                return
            mime_type = await sync_to_async(get_mime_type, path)
            files, folders = 1, 0
        try:
            remote_opts = await self._get_remote_options(oconfig_path, oremote)
        except Exception as err:
            await self._listener.onUploadError(err)
            return
        remote_type = remote_opts["type"]
        fremote = oremote
        fconfig_path = oconfig_path
        using_sa = (
            remote_type == "drive"
            and config_dict["USE_SERVICE_ACCOUNTS"]
            and fconfig_path == "rclone.conf"
            and await aiopath.isdir("accounts")
        )
        if using_sa:
            fconfig_path = await self._create_rc_sa(oremote, remote_opts)
            if fconfig_path != "rclone.conf":
                sa_files = await listdir("accounts")
                self._sa_number = len(sa_files)
                self._sa_index = randrange(self._sa_number)
                fremote = f"sa{self._sa_index:03}"
                LOGGER.info("Upload with service account %s", fremote)
        method = "move" if not self._listener.seed or self._listener.newDir else "copy"
        cmd = self._getUpdatedCommand(
            fconfig_path, path, f"{fremote}:{rc_path}", method
        )
        if (
            remote_type == "drive"
            and not config_dict["RCLONE_FLAGS"]
            and not self._listener.rcFlags
        ):
            cmd.extend(("--drive-chunk-size", "128M", "--drive-upload-cutoff", "128M"))
        result = await self._start_upload(cmd, remote_type, using_sa)
        if not result:
            return
        if remote_type == "drive":
            link, destination = await self._get_gdrive_link(
                oconfig_path, oremote, rc_path, mime_type
            )
        else:
            if mime_type == "Folder":
                destination = f"{oremote}:{rc_path}"
            elif rc_path:
                destination = f"{oremote}:{rc_path}/{self._listener.name}"
            else:
                destination = f"{oremote}:{self._listener.name}"
            cmd = ["rclone", "link", "--config", oconfig_path, destination]
            res, err, code = await cmd_exec(cmd)
            if code == 0:
                link = res
            elif code != -9:
                LOGGER.error(
                    "While getting link. Path: %s | Stderr: %s", destination, err
                )
                link = ""
        if self._is_cancelled:
            return
        LOGGER.info("Upload Done. Path: %s", destination)
        await self._listener.onUploadComplete(
            link, size, files, folders, mime_type, destination
        )

    async def clone(self, config_path, src_remote, src_path, mime_type, drive_id):
        destination = self._listener.upDest
        dst_remote, dst_path = destination.split(":", 1)
        if drive_id:
            try:
                dst_remote_opt = await self._get_remote_options(config_path, dst_remote)
            except Exception as err:
                await self._listener.onUploadError(err)
                return None, None
            src_remote_type, dst_remote_type = "drive", dst_remote_opt["type"]
            if dst_remote_type != "drive":
                await self._listener.onUploadError(
                    "Destion not supported for clone gdrive link!"
                )
                return None, None
            if mime_type == "Folder":
                dst_path = self._listener.name
                method, scr_path, destination = (
                    "copy",
                    f"{dst_remote}:{{{drive_id}}}",
                    f"{destination}/{self._listener.name}",
                )
            else:
                method, scr_path, destination = (
                    ["backend", "copyid"],
                    f"{dst_remote}:",
                    [drive_id, f"{destination}/"],
                )
            cmd = self._getUpdatedCommand(config_path, scr_path, destination, method)
        else:
            try:
                src_remote_opts, dst_remote_opt = await gather(
                    self._get_remote_options(config_path, src_remote),
                    self._get_remote_options(config_path, dst_remote),
                )
            except Exception as err:
                await self._listener.onUploadError(err)
                return None, None
            src_remote_type, dst_remote_type = (
                src_remote_opts["type"],
                dst_remote_opt["type"],
            )
            cmd = self._getUpdatedCommand(
                config_path, f"{src_remote}:{src_path}", destination, "copy"
            )

        if (
            dst_remote_type == "drive"
            and config_path == "rclone.conf"
            and config_dict["USE_SERVICE_ACCOUNTS"]
            and await aiopath.isdir("accounts")
        ):
            LOGGER.info("RClone with service accounts.")
            cmd.extend(
                (
                    "--drive-random-pick-sa",
                    "--drive-rolling-sa",
                    "--drive-rolling-count",
                    "4",
                )
            )
        if not self._listener.rcFlags and not config_dict["RCLONE_FLAGS"]:
            if src_remote_type == "drive" and dst_remote_type != "drive":
                cmd.append("--drive-acknowledge-abuse")
            elif src_remote_type == "drive":
                cmd.extend(("--tpslimit", "3"))
            if src_remote_type == dst_remote_type:
                cmd.extend(
                    (
                        "--server-side-across-configs",
                        "--drive-server-side-across-configs",
                    )
                )

        self._proc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        _, return_code = await gather(self._progress(), self._proc.wait())
        if self._is_cancelled:
            return None, None
        if return_code == -9:
            return None, None
        if return_code != 0:
            error = (await self._proc.stderr.read()).decode().strip()
            if (
                not error
                and drive_id
                and dst_remote_type == "drive"
                and config_dict["USE_SERVICE_ACCOUNTS"]
            ):
                error = "Mostly your service accounts don't have acces to this drive!"
            LOGGER.error(error)
            await self._listener.onUploadError(error)
            return None, None

        if dst_remote_type == "drive":
            link, destination = await self._get_gdrive_link(
                config_path, dst_remote, dst_path, mime_type
            )
            return (None, None) if self._is_cancelled else (link, destination)
        if mime_type != "Folder":
            destination += (
                f"/{self._listener.name}" if dst_path else self._listener.name
            )
        cmd = ["rclone", "link", "--config", config_path, destination]
        res, err, code = await cmd_exec(cmd)
        if self._is_cancelled:
            return None, None
        if code == 0:
            return res, destination
        if code != -9:
            LOGGER.error("While getting link. Path: %s | Stderr: %s", destination, err)
            return None, destination

    def _getUpdatedCommand(self, config_path, source, destination, method):
        ext = "*.{" + ",".join(self._listener.extensionFilter) + "}"
        cmd = ["rclone", "--fast-list", "--config", config_path, "-P"]
        cmd.extend(method) if isinstance(method, list) else cmd.append(method)
        cmd.append(source)
        cmd.extend(destination) if isinstance(destination, list) else cmd.append(
            destination
        )
        cmd.extend(
            (
                "--transfers",
                str(config_dict["RCLONE_TFSIMULATION"]),
                "--exclude",
                ext,
                "--retries-sleep",
                "3s",
                "--ignore-case",
                "--low-level-retries",
                "1",
                "-M",
                "--log-file",
                "rlog.txt",
                "--log-level",
                "DEBUG",
            )
        )
        if rcflags := self._listener.rcFlags or config_dict["RCLONE_FLAGS"]:
            rcflags = rcflags.split("|")
            for flag in rcflags:
                if ":" in flag:
                    key, value = map(str.strip, flag.split(":", 1))
                    cmd.extend((key, value))
                elif len(flag) > 0:
                    cmd.append(flag.strip())
        return cmd

    @staticmethod
    async def _get_remote_options(config_path, remote):
        config = ConfigParser()
        async with aiopen(config_path, "r") as f:
            contents = await f.read()
            config.read_string(contents)
        options = config.options(remote)
        return {opt: config.get(remote, opt) for opt in options}

    async def cancel_task(self):
        self._is_cancelled = True
        if self._proc:
            try:
                self._proc.kill()
            except:
                pass
        if self._is_download:
            LOGGER.info("Cancelling Download: %s", self._listener.name)
            await self._listener.onDownloadError("Download stopped by user!")
        elif self._is_upload:
            LOGGER.info("Cancelling Upload: %s", self._listener.name)
            await self._listener.onUploadError("Upload stopped by user!")
        else:
            LOGGER.info("Cancelling Clone: %s", self._listener.name)
            await self._listener.onUploadError("Clone stopped by user!")
