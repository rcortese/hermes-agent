import { describe, expect, it } from "vitest";
import { sortProfilesForSelector } from "@/contexts/ProfileProvider";
import type { ProfileInfo } from "@/lib/api";

function profile(
  name: string,
  extras: Partial<ProfileInfo> = {},
): ProfileInfo {
  return {
    name,
    path: null,
    is_default: false,
    model: null,
    provider: null,
    has_env: false,
    skill_count: 0,
    gateway_running: false,
    ...extras,
  };
}

describe("sortProfilesForSelector", () => {
  it("shows remote persona profiles before local profiles and sorts each group", () => {
    const result = sortProfilesForSelector([
      profile("z-local"),
      profile("moss", { remote_proxy: true, label: "Moss" }),
      profile("a-local"),
      profile("jen", { profile_kind: "remote_gateway_proxy", label: "Jen" }),
    ]);

    expect(result.map(({ name }) => name)).toEqual([
      "jen",
      "moss",
      "a-local",
      "z-local",
    ]);
    expect(result[0]?.label).toBe("Jen");
  });
});
