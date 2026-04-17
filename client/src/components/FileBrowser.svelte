<script lang="ts">
  import { onMount } from "svelte";
  import { getFiles, getRoots, getHidden, getMarkers, hideFile, unhideFile } from "$lib/api";
  import {
    files, roots, hiddenFiles, currentFile, showHidden,
    profile, markers, visibleFiles
  } from "$lib/stores";

  let selectedRoot = $state("");
  let currentFolder = $state("");

  onMount(async () => {
    $roots = await getRoots();
    if ($roots.length) {
      selectedRoot = $roots[0];
      await loadFiles();
    }
  });

  // Reload hidden files when profile changes
  $effect(() => {
    void $profile;
    if (selectedRoot) {
      loadFiles();
    }
  });

  async function loadFiles() {
    $files = await getFiles(selectedRoot);
    const hidden = await getHidden($profile);
    $hiddenFiles = new Set(hidden);
  }

  // Derive subfolders and files at current folder level
  let subfolders = $derived.by(() => {
    const prefix = currentFolder ? currentFolder + "/" : "";
    const folderSet = new Set<string>();
    for (const f of $visibleFiles) {
      if (!f.path.startsWith(prefix)) continue;
      const rest = f.path.slice(prefix.length);
      const slashIdx = rest.indexOf("/");
      if (slashIdx !== -1) {
        folderSet.add(rest.slice(0, slashIdx));
      }
    }
    return [...folderSet].sort();
  });

  let currentFiles = $derived.by(() => {
    const prefix = currentFolder ? currentFolder + "/" : "";
    return $visibleFiles.filter(f => {
      if (!f.path.startsWith(prefix)) return false;
      const rest = f.path.slice(prefix.length);
      return !rest.includes("/"); // only direct children
    });
  });

  async function selectFile(file: typeof $files[0]) {
    $currentFile = file;
    $markers = await getMarkers(file.name, $profile);
  }

  function navigateToFolder(name: string) {
    currentFolder = currentFolder ? currentFolder + "/" + name : name;
  }

  function navigateUp() {
    const idx = currentFolder.lastIndexOf("/");
    currentFolder = idx === -1 ? "" : currentFolder.slice(0, idx);
  }

  function formatSize(bytes: number): string {
    if (bytes > 1e9) return (bytes / 1e9).toFixed(1) + " GB";
    if (bytes > 1e6) return (bytes / 1e6).toFixed(0) + " MB";
    return (bytes / 1e3).toFixed(0) + " KB";
  }

  async function toggleHidden(file: typeof $files[0]) {
    if ($hiddenFiles.has(file.name)) {
      await unhideFile(file.name, $profile);
    } else {
      await hideFile(file.name, $profile);
    }
    await loadFiles();
  }
</script>

<div class="file-browser">
  <div class="controls">
    <select bind:value={selectedRoot} onchange={() => { currentFolder = ""; loadFiles(); }}>
      {#each $roots as root}
        <option value={root}>{root}</option>
      {/each}
    </select>
    <label><input type="checkbox" bind:checked={$showHidden} /> Hidden</label>
  </div>
  {#if currentFolder}
    <div class="breadcrumb" onclick={navigateUp}>.. / {currentFolder}</div>
  {/if}
  <ul class="file-list">
    {#each subfolders as folder}
      <li class="folder" onclick={() => navigateToFolder(folder)}>
        <span class="name">{folder}/</span>
        <span class="badge">dir</span>
      </li>
    {/each}
    {#each currentFiles as file}
      <li
        class:selected={$currentFile?.path === file.path}
        onclick={() => selectFile(file)}
        oncontextmenu={(e) => { e.preventDefault(); toggleHidden(file); }}
      >
        <span class="name">{file.name}</span>
        <span class="size">{formatSize(file.size)}</span>
      </li>
    {/each}
  </ul>
</div>

<style>
  .file-browser {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-width: 200px;
  }
  .controls {
    display: flex;
    gap: 4px;
    padding: 4px;
    align-items: center;
  }
  .controls select {
    flex: 1;
    background: #2d2d2d;
    color: #e0e0e0;
    border: 1px solid #444;
    padding: 2px;
  }
  .breadcrumb {
    padding: 3px 8px;
    font-size: 11px;
    color: #88aaff;
    cursor: pointer;
    background: #252525;
    border-bottom: 1px solid #333;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .breadcrumb:hover { background: #2a2a2a; }
  .file-list {
    list-style: none;
    padding: 0;
    margin: 0;
    overflow-y: auto;
    flex: 1;
  }
  .file-list li {
    padding: 4px 8px;
    cursor: pointer;
    display: flex;
    justify-content: space-between;
    font-size: 12px;
    white-space: nowrap;
  }
  .file-list li:hover { background: #333; }
  .file-list li.selected { background: #0066cc; }
  .file-list li.folder { color: #88aaff; }
  .name { flex: 1; overflow: hidden; text-overflow: ellipsis; }
  .size { flex-shrink: 0; margin-left: 8px; color: #888; font-size: 11px; }
  .badge { flex-shrink: 0; margin-left: 8px; color: #666; font-size: 10px; }
</style>
