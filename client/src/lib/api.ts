const DEFAULT_SERVER = "http://192.168.1.51:8000";

let serverUrl = DEFAULT_SERVER;

export function setServer(url: string) {
  serverUrl = url.replace(/\/+$/, "");
}

export function getServer(): string {
  return serverUrl;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${serverUrl}${path}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${serverUrl}${path}`, {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function del<T>(path: string): Promise<T> {
  const res = await fetch(`${serverUrl}${path}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

// --- Files ---

export interface VideoFile {
  name: string;
  path: string;
  root: string;
  size: number;
}

export function getRoots(): Promise<string[]> {
  return get("/api/roots");
}

export function getFiles(root?: string): Promise<VideoFile[]> {
  const q = root ? `?root=${encodeURIComponent(root)}` : "";
  return get(`/api/files${q}`);
}

// For {path:path} routes, encode each segment individually to preserve slashes
function encodePath(p: string): string {
  return p.split("/").map(encodeURIComponent).join("/");
}

export function streamUrl(path: string, root: string, quality: string): string {
  return `${serverUrl}/api/stream/${encodePath(path)}?root=${encodeURIComponent(root)}&quality=${quality}`;
}

export function audioUrl(path: string, root: string): string {
  return `${serverUrl}/api/audio/${encodePath(path)}?root=${encodeURIComponent(root)}`;
}

export function cacheStatus(path: string, root: string): Promise<Record<string, string>> {
  return get(`/api/cache/status/${encodePath(path)}?root=${encodeURIComponent(root)}`);
}

// --- Markers & Profiles ---

export interface Marker {
  start_time: number;
  marker_number: number;
  output_path: string;
}

export function getMarkers(filename: string, profile: string = "default"): Promise<Marker[]> {
  return get(`/api/markers/${encodeURIComponent(filename)}?profile=${encodeURIComponent(profile)}`);
}

export function getProfiles(): Promise<string[]> {
  return get("/api/profiles");
}

export function getLabels(): Promise<string[]> {
  return get("/api/labels");
}

// --- Export ---

export interface ExportRequest {
  input_path: string;
  cursor: number;
  name: string;
  clips?: number;
  spread?: number;
  short_side?: number | null;
  portrait_ratio?: string | null;
  crop_center?: number;
  format?: string;
  label?: string;
  category?: string;
  profile?: string;
  folder_suffix?: string;
  encoder?: string;
}

export function startExport(req: ExportRequest): Promise<{ job_id: string }> {
  return post("/api/export", req);
}

export function getExportStatus(jobId: string): Promise<{
  status: string;
  total: number;
  completed: number;
  outputs: string[];
  error?: string;
}> {
  return get(`/api/export/${jobId}`);
}

export function deleteExport(outputPath: string): Promise<{ deleted: string }> {
  return del(`/api/export?output_path=${encodeURIComponent(outputPath)}`);
}

// --- Hidden ---

export function hideFile(filename: string, profile: string = "default"): Promise<unknown> {
  return post(`/api/hidden/${encodeURIComponent(filename)}?profile=${encodeURIComponent(profile)}`);
}

export function unhideFile(filename: string, profile: string = "default"): Promise<unknown> {
  return del(`/api/hidden/${encodeURIComponent(filename)}?profile=${encodeURIComponent(profile)}`);
}

export function getHidden(profile: string = "default"): Promise<string[]> {
  return get(`/api/hidden?profile=${encodeURIComponent(profile)}`);
}
