from time import time

from bot.helper.ext_utils.bot_utils import async_to_sync
from bot.helper.ext_utils.files_utils import get_path_size
from bot.helper.ext_utils.status_utils import (
    MirrorStatus,
    get_readable_file_size,
    get_readable_time,
)


class YtDlpDownloadStatus:
    def __init__(self, listener, obj, gid):
        self._obj = obj
        self._gid = gid
        self._elapsed = time()
        self.listener = listener

    @staticmethod
    def engine():
        return "YT-DLP"

    def elapsed(self):
        return get_readable_time(time() - self._elapsed)

    def gid(self):
        return self._gid

    def processed_bytes(self):
        return get_readable_file_size(self.processed_raw())

    def processed_raw(self):
        return (
            self._obj.downloaded_bytes
            if self._obj.downloaded_bytes != 0
            else async_to_sync(get_path_size, self.listener.dir)
        )

    def size(self):
        return get_readable_file_size(self._obj.size)

    @staticmethod
    def status():
        return MirrorStatus.STATUS_DOWNLOADING

    def name(self):
        return self.listener.name

    def progress(self):
        return f"{round(self._obj.progress, 2)}%"

    def speed(self):
        return f"{get_readable_file_size(self._obj.download_speed)}/s"

    def eta(self):
        if self._obj.eta != "~":
            return get_readable_time(self._obj.eta)
        try:
            return get_readable_time(
                (self._obj.size - self.processed_raw()) / self._obj.download_speed
            )
        except:
            return "~"

    def task(self):
        return self._obj
