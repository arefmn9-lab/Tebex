import fs from "node:fs";

const page = fs.readFileSync(new URL("../src/pages/BaleAccounts.jsx", import.meta.url), "utf8");
const api = fs.readFileSync(new URL("../src/api/baleOnboarding.js", import.meta.url), "utf8");
const presentation = fs.readFileSync(new URL("../src/baleAuthPresentation.js", import.meta.url), "utf8");

const checks = [
  ["dynamic account rendering", page.includes("visible.map((account)")],
  ["search and filter", page.includes('aria-label="جستجوی اکانت"') && page.includes('aria-label="فیلتر وضعیت"')],
  ["account creation remains available", page.includes("validateAndProvision") && page.includes("provisionBaleAccount")],
  ["normal actions are present on every row", page.includes('className="row-actions account-actions account-primary-actions"') && page.includes('openLogin(account, "login")') && page.includes('openLogin(account, "session_recheck")') && page.includes('runAccountAction(account, "delete_account")')],
  ["reset is secondary", page.includes("account-more-actions") && page.includes('runAccountAction(account, "reset_profile")')],
  ["raw runtime details are absent from rows", !page.includes("account.canonical_profile_path") && !page.includes("account.browser_provider") && !page.includes("account.profile_id")],
  ["internal maintenance controls are not rendered", page.includes("{/*") && page.includes("*/}")],
  ["compact human statuses", ["checking_session", "session_problem", "busy", "login_required", "ready", "error"].every((name) => presentation.includes(`${name}:`))],
  ["account errors are operator-facing", page.includes("formatBaleAccountActionError(status") && page.includes("formatBaleAccountActionError(account.last_error)") && presentation.includes("formatBaleAccountActionError")],
  ["session recheck uses its own endpoint", page.includes('purpose === "session_recheck" ? "/automation/platforms/bale/authentication/session-recheck"') && api.includes("/authentication/session-recheck")],
  ["per-action polling refreshes canonical state", page.includes("startPolling(account.account_id, result.operation_id, action)") && page.includes("await refresh()")],
  ["no OTP input", !/<input[^>]+(?:name|id)=["'][^"']*otp/i.test(page) && !/<input[^>]+type=["']password/i.test(page)],
  ["no fixed account slots", !/account_[1-8]\b/.test(page)],
];

const failed = checks.filter(([, passed]) => !passed);
for (const [name, passed] of checks) console.log(`${passed ? "PASS" : "FAIL"} ${name}`);
if (failed.length) process.exit(1);
