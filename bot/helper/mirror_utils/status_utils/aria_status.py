from asyncio import gather
from time import time

from bot import aria2, LOGGER
from bot.helper.ext_utils.bot_utils import sync_to_async
from bot.helper.ext_utils.status_utils import MirrorStatus, get_readable_time


def get_download(gid, old_info=None):
    try:
        return aria2.get_download(gid)
    except Exception as e:
        LOGGER.error("%s: Aria2c, Error while getting torrent info", e)
        return old_info


class Aria2Status:
    def __init__(self, listener, gid, seeding=False, queued=False):
        self._gid = gid
        self._download = None
        self._elapsed = time()
        self.listener = listener
        self.queued = queued
        self.start_time = 0
        self.seeding = seeding

    @staticmethod
    def engine():
        return "Aria2"

    def elapsed(self):
        return get_readable_time(time() - self._elapsed)

    def _update(self):
        if not self._download:
            self._download = get_download(self._gid, self._download)
        else:
            self._download = self._download.live

        if self._download.followed_by_ids:
            self._gid = self._download.followed_by_ids[0]
            self._download = get_download(self._gid)

    def progress(self):
        return self._download.progress_string()

    def processed_bytes(self):
        return self._download.completed_length_string()

    def speed(self):
        return self._download.download_speed_string()

    def name(self):
        return self._download.name

    def size(self):
        return self._download.total_length_string()

    def eta(self):
        return self._download.eta_string()

    def status(self):
        self._update()
        if self._download.is_waiting or self.queued:
            return (
                MirrorStatus.STATUS_QUEUEUP
                if self.seeding
                else MirrorStatus.STATUS_QUEUEDL
            )
        if self._download.is_paused:
            return MirrorStatus.STATUS_PAUSED
        if self._download.seeder and self.seeding:
            return MirrorStatus.STATUS_SEEDING
        return MirrorStatus.STATUS_DOWNLOADING

    def seeders_num(self):
        return self._download.num_seeders

    def leechers_num(self):
        return self._download.connections

    def uploaded_bytes(self):
        return self._download.upload_length_string()

    def upload_speed(self):
        self._update()
        return self._download.upload_speed_string()

    def ratio(self):
        return f"{round(self._download.upload_length / self._download.completed_length, 3)}"

    def seeding_time(self):
        return get_readable_time(time() - self.start_time)

    def task(self):
        return self

    def gid(self):
        self._update()
        return self._gid

    async def cancel_task(self):
        if self._download.seeder and self.seeding:
            LOGGER.info("Cancelling Seed: %s", self.name())
            await gather(
                self.listener.onUploadError(
                    f"Seeding stopped with Ratio: {self.ratio()} and Time: {self.seeding_time()}"
                ),
                sync_to_async(aria2.remove, [self._download], force=True, files=True),
            )
        elif downloads := self._download.followed_by:
            LOGGER.info("Cancelling Download: %s")
            await self.listener.onDownloadError("Download cancelled by user!")
            downloads.append(self._download)
            await sync_to_async(aria2.remove, downloads, force=True, files=True)
        else:
            if self.queued:
                LOGGER.info("Cancelling QueueDl: %s", self.name())
                msg = "Task have been removed from queue/download"
            else:
                LOGGER.info("Cancelling Download: %s", self.name())
                msg = "Download stopped by user!"
            await gather(
                self.listener.onDownloadError(msg),
                sync_to_async(aria2.remove, [self._download], force=True, files=True),
            )
