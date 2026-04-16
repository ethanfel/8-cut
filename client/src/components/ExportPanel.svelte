<script lang="ts" module>
  // Module-level export so App can call doExport via bind:this
</script>

<script lang="ts">
  import { startExport } from "$lib/api";
  import {
    currentFile, cursor, clips, spread, shortSide, portraitRatio,
    cropCenter, format, label, category, clipName, profile,
    hwEncode,
    exportStatus, exportCompleted, exportTotal, subprofiles
  } from "$lib/stores";

  const CATEGORIES = ["", "Human", "Animal", "Vehicle", "Tool", "Music", "Nature", "Sport", "Other"];
  const RATIOS = ["Off", "9:16", "4:5", "1:1"];

  export async function doExport(folderSuffix: string = "") {
    if (!$currentFile) return;
    $exportStatus = "running";
    $exportCompleted = 0;
    $exportTotal = $clips;

    const req = {
      input_path: `${$currentFile.root}/${$currentFile.path}`,
      cursor: $cursor,
      name: $clipName || $currentFile.name.replace(/\.[^.]+$/, ""),
      clips: $clips,
      spread: $spread,
      short_side: $shortSide,
      portrait_ratio: $portraitRatio,
      crop_center: $cropCenter,
      format: $format,
      label: $label,
      category: $category,
      profile: $profile,
      folder_suffix: folderSuffix,
      encoder: $hwEncode ? "h264_nvenc" : "libx264",
    };

    try {
      await startExport(req);
    } catch (e) {
      $exportStatus = "error";
      console.error(e);
    }
  }
</script>

<div class="export-panel">
  <div class="row">
    <button onclick={() => doExport()} disabled={$exportStatus === "running"}>
      Export{#if $exportStatus === "running"} ({$exportCompleted}/{$exportTotal}){/if}
    </button>
    {#each $subprofiles as sub}
      <button onclick={() => doExport(sub)} title="Export {sub}">
        {sub}
      </button>
    {/each}
  </div>

  <div class="row">
    <label>Clips <input type="number" bind:value={$clips} min="1" max="99" /></label>
    <label>Spread <input type="number" bind:value={$spread} min="2" max="8" step="0.5" /></label>
    <label>Size <input type="number" bind:value={$shortSide} min="0" max="4320" step="64" /></label>
    <label>Ratio
      <select bind:value={$portraitRatio}>
        {#each RATIOS as r}
          <option value={r === "Off" ? null : r}>{r}</option>
        {/each}
      </select>
    </label>
  </div>

  <div class="row">
    <label>Label <input type="text" bind:value={$label} /></label>
    <label>Category
      <select bind:value={$category}>
        {#each CATEGORIES as c}
          <option value={c}>{c || "---"}</option>
        {/each}
      </select>
    </label>
    <label>Format
      <select bind:value={$format}>
        <option>MP4</option>
        <option>WebP sequence</option>
      </select>
    </label>
    <label><input type="checkbox" bind:checked={$hwEncode} /> GPU</label>
  </div>
</div>

<style>
  .export-panel {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 4px;
    font-size: 12px;
  }
  .row {
    display: flex;
    gap: 6px;
    align-items: center;
    flex-wrap: wrap;
  }
  label { display: flex; align-items: center; gap: 2px; }
  input[type="number"] { width: 50px; background: #2d2d2d; color: #e0e0e0; border: 1px solid #444; }
  input[type="text"] { width: 120px; background: #2d2d2d; color: #e0e0e0; border: 1px solid #444; }
  select { background: #2d2d2d; color: #e0e0e0; border: 1px solid #444; }
  button { background: #0066cc; color: white; border: none; padding: 4px 12px; cursor: pointer; }
  button:disabled { background: #444; }
</style>
