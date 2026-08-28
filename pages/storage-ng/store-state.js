/**
 * Store initial state - data-only constant (no closures, no behavior).
 *
 * Split from store.js to respect the <=300-line file rule (HL-18); the
 * single source of truth for mutation still lives in store.js. Never
 * attach undeclared keys at runtime (FE-2).
 *
 * @module store-state
 */

export const initialState = {
  // ---- Shell / routing ----
  currentView: 'files',          // active tab id (8-tab IA)
  currentGroup: '',              // '' = aggregated view over all groups (D-3)
  layout: (() => {
    // N-07 rule 3: single pane is the default; explicit choice persists.
    try {
      return typeof localStorage !== 'undefined'
        && localStorage.getItem('cs_layout') === 'dual' ? 'dual' : 'single';
    } catch (e) { return 'single'; }
  })(),

  // ---- Files tab (T-1) ----
  fileType: '',                  // 13-class chip value ('' = all)
  fileStatus: '',                // derived storage-state filter (N-02)
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
  // N-07 rule 2: default sort = modified time, newest first
  fileSort: { by: 'created_at', dir: 'desc' },
  searchQuery: '',               // full-filename search
  tagFilter: '',                 // #tag filter for group files
  folders: [],                   // folder rows of the current listing
  tags: [],                      // tag cloud of the current listing

  // ---- Albums tab (T-2): independent per-module state (D-3) ----
  albumItems: [],
  albumTotal: 0,
  albumSelected: new Set(),
  albumGroup: '',
  albumQuery: '',
  albumTagFilter: '',
  albumTagCloud: [],

  // ---- Essence tab (T-3): independent per-module state (D-3) ----
  essenceItems: [],
  essenceTotal: 0,
  essenceSelected: new Set(),
  essenceGroup: '',
  essenceQuery: '',
  essenceTagFilter: '',
  essenceTagCloud: [],

  // ---- Netdisk tab (T-4) ----
  netdiskFiles: [],
  netdiskPath: '/',
  netdiskPage: 1,
  netdiskTotal: 0,
  netdiskType: '',               // module-isolated netdisk type chip
  netdiskSelected: new Set(),
  bridgeStatus: {},
  currentBridgeDirection: 'out', // 'out' = to netdisk, 'in' = to group
  tasks: [],                     // bridge transfer tasks

  // ---- Tasks tab (T-5) ----
  taskLedger: [],
  taskStateFilter: '',

  // ---- Groups tab (T-6) ----
  groups: [],
  removedGroups: [],
  accounts: [],
  accountFilter: '',
  groupSort: { key: 'sort_order', dir: 'asc' },
  groupPage: 1,
  groupPageSize: 10,
  groupsView: 'active',          // 'active' | 'removed'

  // ---- Config tab (T-7) ----
  configGroups: [],
  configReloadRequired: [],

  // ---- Cross-cutting ----
  stats: {},                     // aggregate stat card payload
  extTypes: null,                // CT-9 classification table (ext -> type)
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