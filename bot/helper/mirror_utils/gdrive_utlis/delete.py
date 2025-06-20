from googleapiclient.errors import HttpError
from logging import getLogger

from bot.helper.mirror_utils.gdrive_utlis.helper import GoogleDriveHelper

LOGGER = getLogger(__name__)


class gdDelete(GoogleDriveHelper):
    def __init__(self):
        super().__init__()

    def deletefile(self, link, user_id):
        try:
            file_id = self.getIdFromUrl(link, user_id)
        except (KeyError, IndexError):
            return "Google Drive ID could not be found in the provided link"
        self.service = self.authorize()
        msg = ""
        try:
            self.service.files().delete(
                fileId=file_id, supportsAllDrives=True
            ).execute()
            msg = "Successfully deleted!"
            LOGGER.info("Delete Result: %s", msg)
        except HttpError as err:
            if "File not found" in str(err) or "insufficientFilePermissions" in str(
                err
            ):
                if not self.alt_auth and self.use_sa:
                    self.alt_auth = True
                    self.use_sa = False
                    LOGGER.error("File not found. Trying with token.pickle...")
                    return self.deletefile(link, user_id)
                err = "File not found or insufficientFilePermissions!"
            LOGGER.error("Delete Result: %s", err)
            msg = str(err)
        return msg
