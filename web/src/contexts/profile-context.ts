import { createContext } from "react";
import type { ProfileInfo } from "@/lib/api";

export type ProfileOption = Pick<
  ProfileInfo,
  "name" | "label" | "remote_proxy" | "profile_kind"
>;

export interface ProfileContextValue {
  /** Profile every management surface reads/writes ("" = the dashboard
   *  process's own profile). */
  profile: string;
  /** The profile the dashboard process itself runs under. */
  currentProfile: string;
  /** Known profiles in selector display order (includes "default"). */
  profiles: ProfileOption[];
  setProfile: (name: string) => void;
}

export const ProfileContext = createContext<ProfileContextValue>({
  profile: "",
  currentProfile: "default",
  profiles: [],
  setProfile: () => {},
});
