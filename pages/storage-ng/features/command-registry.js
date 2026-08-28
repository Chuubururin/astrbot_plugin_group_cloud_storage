/**
 * Command aggregator - registers every command domain exactly once.
 *
 * The action bar calls registerAllCommands() at module load (idempotent);
 * per-domain registration modules delegate here to respect the <=300-line
 * file rule (HL-18). rowGroup is re-exported for the netdisk domain.
 *
 * @module features/command-registry
 */

import { registerAllCommands as registerFilesCommands } from './command-defs.js';
import { registerDistributeCommands } from './distribute.js';
import { registerAllNetdiskCommands } from './command-defs-netdisk.js';

/** Register the complete command registry (safe to call more than once). */
export function registerAllCommands() {
  registerFilesCommands();
  registerAllNetdiskCommands();
  registerDistributeCommands();
}

export { rowGroup } from './commands.js';
export { copyToClipboard } from '../utils/helpers.js';