/**
 * Backend default classification table mirror for unit tests.
 * Keep in sync with core/domain/file_type.py FILE_TYPE_EXT (covered by
 * pytest tests/unit/test_preview_policy.py as the authoritative source).
 */
export const FILE_TYPE_EXT = {
  document: ['.doc', '.docx', '.pdf', '.ppt', '.pptx', '.xls', '.xlsx',
    '.txt', '.md', '.odt', '.rtf'],
  image: ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg', '.heic'],
  audio: ['.mp3', '.wav', '.flac', '.ogg', '.m4a', '.aac'],
  video: ['.mp4', '.mkv', '.avi', '.mov', '.flv', '.webm', '.wmv'],
  archive: ['.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz'],
  program: ['.exe', '.msi', '.apk', '.deb', '.rpm', '.dmg', '.appimage'],
  data: ['.db', '.csv', '.json', '.xml', '.sqlite', '.sql'],
};
