import fs from "node:fs";

const page = fs.readFileSync(new URL("../src/pages/BaleAccounts.jsx", import.meta.url), "utf8");
const api = fs.readFileSync(new URL("../src/api/baleOnboarding.js", import.meta.url), "utf8");

const checks = [
  ["dynamic rendering", page.includes("visible.map((account)")],
  ["search", page.includes('aria-label=\"جستجوی اکانت\"')],
  ["filter", page.includes('aria-label=\"فیلتر وضعیت\"')],
  ["pagination", page.includes("pageSize")],
  ["direct account creation", page.includes("validateAndProvision") && page.includes("provisionBaleAccount")],
  ["no batch prerequisite", !page.includes("createBaleOnboardingBatch") && !page.includes("listBaleOnboardingBatches") && !page.includes("batch_id")],
  ["per-account login actions", page.includes("openLogin(account") && page.includes("confirmLogin(account)") && page.includes("closeBrowser(account)")],
  ["permanent profile result", page.includes("canonical_profile_path") && page.includes("بازکردن Chrome برای ورود")],
  ["no OTP input", !/<input[^>]+(?:name|id)=[\"'][^\"']*otp/i.test(page) && !/<input[^>]+type=[\"']password/i.test(page)],
  ["duplicate-click protection", page.includes("disabled={Boolean(busy)}")],
  ["login polling cleanup", page.includes("window.clearInterval")],
  ["refresh resume", page.includes("localStorage.getItem") && page.includes("draftStorageKey") && page.includes("sessionStorageKey")],
  ["disable confirmation", page.includes("window.confirm") && page.includes("disableBaleAccount")],
  ["backend authoritative refresh", page.includes("await refresh()")],
  ["controlled authentication API", api.includes("/authentication/open") && !api.includes("open-login")],
  ["no fixed account slots", !/account_[1-8]\b/.test(page)],
];

const failed = checks.filter(([, passed]) => !passed);
for (const [name, passed] of checks) console.log(`${passed ? "PASS" : "FAIL"} ${name}`);
if (failed.length) process.exit(1);
