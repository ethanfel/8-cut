import {
  serverUrl, quality, clips, spread, shortSide, portraitRatio,
  format, hwEncode, profile, subprofiles
} from "./stores";
import { setServer } from "./api";
import { get } from "svelte/store";

const KEY = "8cut-settings";

interface Settings {
  serverUrl: string;
  quality: string;
  clips: number;
  spread: number;
  shortSide: number | null;
  portraitRatio: string | null;
  format: string;
  hwEncode: boolean;
  profile: string;
  subprofiles: string[];
}

export function saveSettings() {
  const data: Settings = {
    serverUrl: get(serverUrl),
    quality: get(quality),
    clips: get(clips),
    spread: get(spread),
    shortSide: get(shortSide),
    portraitRatio: get(portraitRatio),
    format: get(format),
    hwEncode: get(hwEncode),
    profile: get(profile),
    subprofiles: get(subprofiles),
  };
  localStorage.setItem(KEY, JSON.stringify(data));
}

export function loadSettings() {
  const raw = localStorage.getItem(KEY);
  if (!raw) return;
  try {
    const data: Settings = JSON.parse(raw);
    if (data.serverUrl) {
      serverUrl.set(data.serverUrl);
      setServer(data.serverUrl);
    }
    if (data.quality) quality.set(data.quality);
    if (data.clips) clips.set(data.clips);
    if (data.spread) spread.set(data.spread);
    if (data.shortSide !== undefined) shortSide.set(data.shortSide);
    if (data.portraitRatio !== undefined) portraitRatio.set(data.portraitRatio);
    if (data.format) format.set(data.format);
    if (data.hwEncode !== undefined) hwEncode.set(data.hwEncode);
    if (data.profile) profile.set(data.profile);
    if (data.subprofiles) subprofiles.set(data.subprofiles);
  } catch { /* ignore corrupt settings */ }
}
