import { writable, derived } from "svelte/store";
import type { VideoFile, Marker } from "./api";

// --- Connection ---
export const serverUrl = writable("http://192.168.1.51:8000");

// --- Files ---
export const roots = writable<string[]>([]);
export const files = writable<VideoFile[]>([]);
export const hiddenFiles = writable<Set<string>>(new Set());
export const currentFile = writable<VideoFile | null>(null);
export const hideExported = writable(false);
export const showHidden = writable(false);

// --- Playback ---
export const duration = writable(0);
export const cursor = writable(0);
export const playPos = writable<number | null>(null);
export const playing = writable(false);
export const quality = writable("low");

// --- Timeline ---
export const markers = writable<Marker[]>([]);
export const locked = writable(false);

// --- Export settings ---
export const clips = writable(3);
export const spread = writable(3.0);
export const shortSide = writable<number | null>(512);
export const portraitRatio = writable<string | null>(null);
export const cropCenter = writable(0.5);
export const format = writable("MP4");
export const hwEncode = writable(false);
export const label = writable("");
export const category = writable("");
export const clipName = writable("");
export const exportFolder = writable("");
export const encoder = writable("libx264");
export const trackSubject = writable(false);
export const randPortrait = writable(false);
export const randSquare = writable(false);

// --- Profiles ---
export const profile = writable("default");
export const subprofiles = writable<string[]>([]);

// --- Export progress ---
export const exportStatus = writable<string>("idle"); // idle | running | done | error
export const exportCompleted = writable(0);
export const exportTotal = writable(0);

// --- Derived ---
export const clipSpan = derived(
  [clips, spread],
  ([$clips, $spread]) => 8.0 + ($clips - 1) * $spread
);

export const visibleFiles = derived(
  [files, hiddenFiles, showHidden],
  ([$files, $hidden, $showHidden]) => {
    return $files.filter(f => {
      if (!$showHidden && $hidden.has(f.name)) return false;
      return true;
    });
  }
);
