/**
 * Store initial state - data-only constant (no closures, no behavior).
 *
 * Split from store.js to respect the <=300-line file rule ; the
 * single source of truth for mutation still lives in store.js. Never
 * attach undeclared keys at runtime .
 *
 * @module store-state
 */

export const initialState = {
  // ---- Shell / routing ----
  currentView: 'files',          // active tab id (8-tab IA)
  currentGroup: '',              // '' = aggregated view over all groups
  layout: (() => {
    // Single pane is the default; explicit choice persists.
    try {
      return typeof localStorage !== 'undefined'
        && localStorage.getItem('cs_layout') === 'dual' ? 'dual' : 'single';
    } catch (e) { return 'single'; }
  })(),

  // ---- Files tab  ----
  fileType: '',                  // 13-class chip value ('' = all)
  fileStatus: '',                // derived storage-state filter
  folder: '',                    // current folder name ('' = root; folders are flat, one level)
  folderChain: [],               // breadcrumb chain (single level for group files)
  filePage: 1,                    // group-file page (folder-scoped)
  albumPage: 1,                   // module-isolated album page
  essencePage: 1,                 // module-isolated essence page
  filePageSize: 24,
  fileTotal: 0,
  fileItems: [],
  fileSelected: new Set(),
  fileSelRows: new Map(),
  // Default sort = modified time, newest first
  fileSort: { by: 'created_at', dir: 'desc' },
  searchQuery: '',               // full-filename search
  tagFilter: '',                 // #tag filter for group files
  folders: [],                   // folder rows of the current listing
  tags: [],                      // tag cloud of the current listing

  // ---- Albums tab : independent per-module state  ----
  albumItems: [],
  albumTotal: 0,
  albumSelected: new Set(),
  albumGroup: undefined,
  albumQuery: '',
  albumTagFilter: '',
  albumTagCloud: [],

  // ---- Essence tab : independent per-module state  ----
  essenceItems: [],
  essenceTotal: 0,
  essenceSelected: new Set(),
  essenceGroup: undefined,
  essenceQuery: '',
  essenceTagFilter: '',
  essenceTagCloud: [],

  // ---- Netdisk tab  ----
  netdiskFiles: [],
  netdiskPath: '/',
  netdiskPage: 1,
  netdiskTotal: 0,
  netdiskType: '',               // module-isolated netdisk type chip
  netdiskSelected: new Set(),
  bridgeStatus: {},
  currentBridgeDirection: 'out', // 'out' = to netdisk, 'in' = to group
  tasks: [],                     // bridge transfer tasks

  // ---- Tasks tab  ----
  taskLedger: [],
  taskStateFilter: '',

  // ---- Groups tab  ----
  groups: [],
  removedGroups: [],
  accounts: [],
  accountFilter: '',
  groupSort: { key: 'sort_order', dir: 'asc' },
  groupPage: 1,
  groupPageSize: 10,
  groupsView: 'active',          // 'active' | 'removed'

  // ---- Config tab  ----
  configGroups: [],
  configReloadRequired: [],

  // ---- Cross-cutting ----
  stats: {},                     // aggregate stat card payload
  extTypes: null,                // classification table (ext -> type)
  queueStatus: {},
  loading: false,
  error: null,
  activeTask: null,              // latest SSE task event (header indicator)
  taskLog: [],                   // floating task panel feed (newest first)
  taskPanelOpen: false,
  sseConnected: true,
  busyKeys: new Set(),           // command ids currently in flight
  reqSeq: {},                    // per-topic sequence for stale-response guard
};