import { request } from "./client";

export async function listAccounts() {
  try {
    return await request("/automation/accounts");
  } catch (error) {
    if (error.status === 404) {
      return [];
    }
    throw error;
  }
}

