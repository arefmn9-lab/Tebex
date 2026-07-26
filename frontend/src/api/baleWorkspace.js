import {
  getBaleBulkResults,
  getBaleBulkResumeState,
  listBaleBulkCampaigns,
  runBaleBulkDryPreflight,
  saveBaleBulkCampaign,
  validateBaleBulkCampaign,
} from "./campaigns";
import {
  checkBaleLogin,
  getBaleJobs,
  getBaleMessageConfig,
  getBaleSourceChannel,
  listPlatformAccounts,
  listPlatformLogs,
  listPlatformTasks,
  openBaleLogin,
  saveBaleMessageConfig,
  saveBaleSourceChannel,
  updateBaleAccount,
  deleteBaleAccount,
} from "./platforms";

export const BALE_MAX_RECIPIENTS_V1 = 10;
export const BALE_CONCURRENCY_V1 = 1;

export function listBaleAccounts() {
  return listPlatformAccounts("bale");
}

export function listBaleTasks() {
  return listPlatformTasks("bale");
}

export function listBaleLogs() {
  return listPlatformLogs("bale");
}

export {
  checkBaleLogin,
  deleteBaleAccount,
  getBaleBulkResults,
  getBaleBulkResumeState,
  getBaleJobs,
  getBaleMessageConfig,
  getBaleSourceChannel,
  listBaleBulkCampaigns,
  openBaleLogin,
  runBaleBulkDryPreflight,
  saveBaleBulkCampaign,
  saveBaleMessageConfig,
  saveBaleSourceChannel,
  updateBaleAccount,
  validateBaleBulkCampaign,
};
