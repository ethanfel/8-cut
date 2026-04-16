<script lang="ts">
  import { onMount } from "svelte";
  import { getFiles, getRoots, getHidden, getMarkers, hideFile, unhideFile } from "$lib/api";
  import {
    files, roots, hiddenFiles, currentFile, showHidden,
    profile, markers, visibleFiles
  } from "$lib/stores";

  let selectedRoot = $state("");

  onMount(async () => {
    $roots = await getRoots();
    if ($roots.length) {
      selectedRoot = $roots[0];
      await loadFiles();
    }
  });

  async function loadFiles() {
    $files = await getFiles(selectedRoot);
    const hidden = await getHidden($profile);
    $hiddenFiles = new Set(hidden);
  }

  async function selectFile(file: typeof $files[0]) {
    $currentFile = file;
    $markers = await getMarkers(file.name, $profile);
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
    <select bind:value={selectedRoot} onchange={loadFiles}>
      {#each $roots as root}
        <option value={root}>{root}</option>
      {/each}
    </select>
    <label><input type="checkbox" bind:checked={$showHidden} /> Hidden</label>
  </div>
  <ul class="file-list">
    {#each $visibleFiles as file}
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
  }
  .file-list li:hover { background: #333; }
  .file-list li.selected { background: #0066cc; }
  .size { color: #888; font-size: 11px; }
</style>
