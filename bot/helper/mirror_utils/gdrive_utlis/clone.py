from googleapiclient.errors import HttpError
from logging import getLogger
from os import path as ospath
from tenacity import (
    retry,
    wait_exponential,
    stop_after_attempt,
    retry_if_exception_type,
    RetryError,
)
from time import time

from bot.helper.ext_utils.bot_utils import async_to_sync
from bot.helper.mirror_utils.gdrive_utlis.helper import GoogleDriveHelper

LOGGER = getLogger(__name__)


class gdClone(GoogleDriveHelper):
    def __init__(self, listener):
        self.listener = listener
        self._start_time = time()
        super().__init__()
        self.is_cloning = True
        self.user_setting()

    def user_setting(self):
        if self.listener.upDest.startswith("mtp:") or self.listener.link.startswith(
            "mtp:"
        ):
            self.token_path = f"tokens/{self.listener.user_id}.pickle"
            self.listener.upDest = self.listener.upDest.replace("mtp:", "", 1)
            self.use_sa = False
        elif self.listener.upDest.startswith("tp:"):
            self.listener.upDest = self.listener.upDest.replace("tp:", "", 1)
            self.use_sa = False
        elif self.listener.upDest.startswith("sa:") or self.listener.user_dict.get(
            "use_sa"
        ):
            self.listener.upDest = self.listener.upDest.replace("sa:", "", 1)
            self.use_sa = True

    def clone(self):
        try:
            file_id = self.getIdFromUrl(self.listener.link, self.listener.user_id)
        except (KeyError, IndexError):
            return (
                "Google Drive ID could not be found in the provided link",
                None,
                None,
                None,
                None,
                None,
            )
        self.service = self.authorize()
        msg = ""
        LOGGER.info("File ID: %s", file_id)
        try:
            meta = self.getFileMetadata(file_id)
            mime_type = meta.get("mimeType")
            if mime_type == self.G_DRIVE_DIR_MIME_TYPE:
                dir_id = self.create_directory(meta.get("name"), self.listener.upDest)
                self._cloneFolder(meta.get("name"), meta.get("id"), dir_id)
                durl = self.G_DRIVE_DIR_BASE_DOWNLOAD_URL.format(dir_id)
                if self.is_cancelled:
                    LOGGER.info("Deleting cloned data from Drive...")
                    self.service.files().delete(
                        fileId=dir_id, supportsAllDrives=True
                    ).execute()
                    return None, None, None, None, None, None
                mime_type = "Folder"
                size = self.proc_bytes
            else:
                self.listener.name = self.listener.newname or self.listener.name
                file = self._copyFile(
                    meta.get("id"), self.listener.upDest, self.listener.newname
                )
                msg += f"<b>Name: </b><code>{file.get('name')}</code>"
                durl = self.G_DRIVE_BASE_DOWNLOAD_URL.format(file.get("id"))
                if mime_type is None:
                    mime_type = "File"
                size = int(meta.get("size", 0))
            return (
                durl,
                size,
                mime_type,
                self.total_files,
                self.total_folders,
                self.getIdFromUrl(durl, self.listener.user_id),
            )
        except Exception as err:
            if isinstance(err, RetryError):
                LOGGER.info("Total Attempts: %s", err.last_attempt.attempt_number)
                err = err.last_attempt.exception()
            err = str(err).replace(">", "").replace("<", "")
            if "User rate limit exceeded" in err:
                msg = "User rate limit exceeded."
            elif "File not found" in err:
                if not self.alt_auth and self.use_sa:
                    self.alt_auth = True
                    self.use_sa = False
                    LOGGER.error("File not found. Trying with token.pickle...")
                    return self.clone()
                msg = "File not found."
            else:
                msg = f"Error.\n{err}"
            async_to_sync(self.listener.onUploadError, msg)
            return None, None, None, None, None, None

    def _cloneFolder(self, folder_name, folder_id, dest_id):
        LOGGER.info("Syncing: %s", folder_name)
        files = self.getFilesByFolderId(folder_id)
        if len(files) == 0:
            return dest_id
        for file in files:
            if file.get("mimeType") == self.G_DRIVE_DIR_MIME_TYPE:
                self.total_folders += 1
                file_path = ospath.join(folder_name, file.get("name"))
                current_dir_id = self.create_directory(file.get("name"), dest_id)
                self._cloneFolder(file_path, file.get("id"), current_dir_id)
            elif (
                not file.get("name")
                .lower()
                .endswith(tuple(self.listener.extensionFilter))
            ):
                self.total_files += 1
                self._copyFile(file.get("id"), dest_id, file.get("name"))
                self.proc_bytes += int(file.get("size", 0))
                self.total_time = int(time() - self._start_time)
            if self.is_cancelled:
                break

    @retry(
        wait=wait_exponential(multiplier=2, min=3, max=6),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(Exception),
    )
    def _copyFile(self, file_id, dest_id, name):
        body = {"parents": [dest_id], "name": name}
        try:
            return (
                self.service.files()
                .copy(fileId=file_id, body=body, supportsAllDrives=True)
                .execute()
            )
        except HttpError as err:
            if err.resp.get("content-type", "").startswith("application/json"):
                reason = eval(err.content).get("error").get("errors")[0].get("reason")
                if reason not in [
                    "userRateLimitExceeded",
                    "dailyLimitExceeded",
                    "cannotCopyFile",
                ]:
                    raise err
                if reason == "cannotCopyFile":
                    LOGGER.error(err)
                elif self.use_sa:
                    if self.sa_count >= self.sa_number:
                        LOGGER.info(
                            "Reached maximum number of service accounts switching, which is %s",
                            self.sa_count,
                        )
                        raise err

                    if self.is_cancelled:
                        return

                    self.switchServiceAccount()
                    return self._copyFile(file_id, dest_id, name)

                LOGGER.error("Got: %s", reason)
                raise err
