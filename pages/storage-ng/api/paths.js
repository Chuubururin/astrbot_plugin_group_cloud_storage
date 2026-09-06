/** API path constants — single source of truth for backend routes. */

const PLUGIN = 'astrbot_plugin_group_cloud_storage';

export const GROUPS = `/${PLUGIN}/groups`;
export const ACCOUNTS = `/${PLUGIN}/accounts`;
export const GROUPS_SCAN = `/${PLUGIN}/groups/scan`;
export const GROUPS_BATCH = `/${PLUGIN}/groups/batch`;
export const GROUPS_BATCH_OPS = `/${PLUGIN}/groups/batch-ops`;
export const GROUPS_ORDER = `/${PLUGIN}/groups/order`;
export const GROUPS_REMOVE = `/${PLUGIN}/groups/remove`;
export const GROUPS_REMOVED = `/${PLUGIN}/groups/removed`;
export const GROUPS_RESTORE = `/${PLUGIN}/groups/restore`;

export const FILES = `/${PLUGIN}/files`;
export const STAT = `/${PLUGIN}/stat`;
export const EVENTS = `/${PLUGIN}/events`;
export const UPLOAD_PREPARE = `/${PLUGIN}/files/upload/prepare`;
export const UPLOAD_EXEC = (token) => `/${PLUGIN}/files/upload/${token}`;
export const RECOMMEND_GROUP = `/${PLUGIN}/files/recommend-group`;
export const FILE_DELETE = `/${PLUGIN}/files/delete`;
export const FILE_REPLACE_NAME = `/${PLUGIN}/files/replace_name`;
export const FILE_MOVE = `/${PLUGIN}/files/move`;
export const FILE_LINK = `/${PLUGIN}/files/link`;
export const FILE_DOWNLOAD = `/${PLUGIN}/files/download`;
export const FILE_URI = `/${PLUGIN}/files/uri`;
export const FILE_DETAIL = `/${PLUGIN}/files/detail`;
export const FILE_TAGS = `/${PLUGIN}/files/tags`;
export const FILE_TAGCLOUD = `/${PLUGIN}/files/tagcloud`;
export const FILE_SCAN = `/${PLUGIN}/files/scan`;
export const FILE_SYNC = `/${PLUGIN}/files/sync`;
export const FILE_FOLDER_CREATE = `/${PLUGIN}/files/folder-create`;
export const FILE_BATCH_DELETE = `/${PLUGIN}/files/batch-delete`;
export const FILE_BATCH_MOVE = `/${PLUGIN}/files/batch-move`;
export const FILE_BATCH_TAGS = `/${PLUGIN}/files/batch-tags`;
export const FILE_LINKS = `/${PLUGIN}/files/links`;

export const ALBUMS_MEDIA = `/${PLUGIN}/albums/media`;
export const ALBUMS_VIDEO_PREVIEW = `/${PLUGIN}/albums/video-preview`;
export const ESSENCE_SAVE = `/${PLUGIN}/essence/save`;
export const ESSENCE_TEXT = `/${PLUGIN}/essence/text`;
export const ESSENCE_DELETE = `/${PLUGIN}/essence/delete`;
export const FETCH = `/${PLUGIN}/fetch`;

export const DOWNLOAD_ADDRESS = `/${PLUGIN}/download/address`;
export const PREVIEW_POLICY = `/${PLUGIN}/preview/policy`;
export const META_CLASSIFY = `/${PLUGIN}/meta/classify`;

export const TASKS = `/${PLUGIN}/tasks`;
export const TASKS_QUEUE = `/${PLUGIN}/tasks/queue`;
export const TASKS_PAUSE = `/${PLUGIN}/tasks/pause`;
export const TASKS_RESUME = `/${PLUGIN}/tasks/resume`;
export const TASKS_INTERRUPT = `/${PLUGIN}/tasks/interrupt`;
export const TASKS_UNDO = `/${PLUGIN}/tasks/undo`;
export const TASKS_OPS = `/${PLUGIN}/tasks/ops`;
export const TASKS_RESUME_PENDING = `/${PLUGIN}/tasks/resume-pending`;

export const CONFIG_GET = `/${PLUGIN}/config/get`;
export const CONFIG_SAVE = `/${PLUGIN}/config/save`;

export const SYNC_WITHERING = `/${PLUGIN}/sync/withering`;
export const SYNC_STATUS = `/${PLUGIN}/sync/status`;
