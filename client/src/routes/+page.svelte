<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import FileBrowser from "../components/FileBrowser.svelte";
  import Timeline from "../components/Timeline.svelte";
  import ExportPanel from "../components/ExportPanel.svelte";
  import ProfileBar from "../components/ProfileBar.svelte";
  import { mpvStart, mpvLoad, mpvSeek, mpvPause, mpvResume, mpvSetLoop, mpvClearLoop, mpvTimePos, mpvDuration } from "$lib/mpv";
  import { streamUrl, audioUrl, deleteExport, getMarkers } from "$lib/api";
  import { connectExportWs } from "$lib/ws";
  import {
    currentFile, cursor, duration, playPos, playing, quality,
    clips, spread, locked, markers, profile, clipSpan, subprofiles
  } from "$lib/stores";

  let pollInterval: ReturnType<typeof setInterval>;
  let exportPanelRef: ExportPanel;

  onMount(async () => {
    await mpvStart();
    connectExportWs();

    // Poll mpv for time position
    pollInterval = setInterval(async () => {
      if ($playing) {
        try {
          $playPos = await mpvTimePos();
        } catch { /* mpv not ready */ }
      }
    }, 50);
  });

  onDestroy(() => {
    clearInterval(pollInterval);
  });

  // Load file into mpv when currentFile OR quality changes
  $effect(() => {
    const file = $currentFile;
    const q = $quality;
    if (file) {
      const vUrl = streamUrl(file.path, file.root, q);
      const aUrl = audioUrl(file.path, file.root);
      mpvLoad(vUrl, aUrl).then(async () => {
        await new Promise(r => setTimeout(r, 500));
        try { $duration = await mpvDuration(); } catch {}
      });
    }
  });

  async function handleCursorChange(time: number) {
    await mpvSeek(time);
  }

  async function handlePlay() {
    const a = $cursor;
    const b = $cursor + $clipSpan;
    await mpvSeek(a);
    await mpvSetLoop(a, b);
    await mpvResume();
    $playing = true;
  }

  async function handlePause() {
    await mpvPause();
    await mpvClearLoop();
    $playing = false;
  }

  async function handleMarkerClick(m: { start_time: number; output_path: string }) {
    if ($locked) {
      const span = 8.0 + ($clips - 1) * $spread;
      $cursor = m.start_time + span;
      await mpvSeek($cursor);
    } else {
      $cursor = m.start_time;
      await mpvSeek(m.start_time);
    }
  }

  async function handleMarkerDelete(outputPath: string) {
    await deleteExport(outputPath);
    if ($currentFile) {
      $markers = await getMarkers($currentFile.name, $profile);
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    const tag = (e.target as HTMLElement).tagName;
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;

    switch (e.key) {
      case " ":
        e.preventDefault();
        $playing ? handlePause() : handlePlay();
        break;
      case "e":
      case "E":
        exportPanelRef?.doExport();
        break;
      case "ArrowLeft":
        $cursor = Math.max(0, $cursor - 1);
        handleCursorChange($cursor);
        break;
      case "ArrowRight":
        $cursor = Math.min($duration, $cursor + 1);
        handleCursorChange($cursor);
        break;
    }

    const num = parseInt(e.key);
    if (num >= 1 && num <= 9) {
      const idx = num - 1;
      if (idx < $subprofiles.length) {
        exportPanelRef?.doExport($subprofiles[idx]);
      }
    }
  }

  function fmtTime(s: number): string {
    const m = Math.floor(s / 60);
    const sec = (Math.floor(s % 60 * 10) / 10).toFixed(1);
    return `${m}:${sec.padStart(4, "0")}`;
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<main>
  <div class="layout">
    <div class="sidebar">
      <FileBrowser />
    </div>
    <div class="content">
      <ProfileBar />
      <div class="player-area">
        <div class="video-placeholder">
          {#if $currentFile}
            <p>{$currentFile.name}</p>
          {:else}
            <p>Select a file</p>
          {/if}
        </div>
      </div>
      <Timeline
        onCursorChange={handleCursorChange}
        onSeek={handleCursorChange}
        onMarkerClick={handleMarkerClick}
        onMarkerDelete={handleMarkerDelete}
      />
      <div class="transport">
        <button onclick={handlePlay} disabled={!$currentFile}>Play</button>
        <button onclick={handlePause}>Pause</button>
        <button onclick={() => $locked = !$locked}>
          {$locked ? "Locked" : "Unlocked"}
        </button>
        <span class="time">
          {#if $duration > 0}
            {fmtTime($cursor)} / {fmtTime($duration)}
          {/if}
        </span>
        <select bind:value={$quality} style="margin-left:auto">
          <option value="potato">480p</option>
          <option value="low">720p</option>
          <option value="medium">1080p</option>
          <option value="high">Original</option>
        </select>
      </div>
      <ExportPanel bind:this={exportPanelRef} />
    </div>
  </div>
</main>

<style>
  :global(body) {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #1e1e1e;
    color: #e0e0e0;
  }
  main { height: 100vh; overflow: hidden; }
  .layout {
    display: flex;
    height: 100%;
  }
  .sidebar {
    width: 220px;
    border-right: 1px solid #333;
    overflow: hidden;
  }
  .content {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .player-area {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #000;
    min-height: 200px;
  }
  .video-placeholder {
    color: #666;
    text-align: center;
  }
  .transport {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 4px 8px;
    background: #222;
  }
  .transport button {
    background: #333;
    color: #e0e0e0;
    border: 1px solid #555;
    padding: 4px 10px;
    cursor: pointer;
  }
  .time {
    font-family: monospace;
    font-size: 13px;
  }
  select { background: #2d2d2d; color: #e0e0e0; border: 1px solid #444; }
</style>
