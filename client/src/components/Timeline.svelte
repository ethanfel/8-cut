<script lang="ts">
  import { onMount } from "svelte";
  import {
    duration, cursor, playPos, markers, clips, spread, locked, clipSpan
  } from "$lib/stores";

  let {
    onCursorChange = (_time: number) => {},
    onSeek = (_time: number) => {},
    onMarkerClick = (_marker: { start_time: number; output_path: string }) => {},
    onMarkerDelete = (_outputPath: string) => {},
  } = $props<{
    onCursorChange?: (time: number) => void;
    onSeek?: (time: number) => void;
    onMarkerClick?: (marker: { start_time: number; output_path: string }) => void;
    onMarkerDelete?: (outputPath: string) => void;
  }>();

  let canvas: HTMLCanvasElement;
  let ctx: CanvasRenderingContext2D;
  let dragging = $state(false);

  const HEIGHT = 160;

  function timeToX(t: number): number {
    if ($duration <= 0) return 0;
    return (t / $duration) * canvas.width;
  }

  function xToTime(x: number): number {
    if ($duration <= 0) return 0;
    return Math.max(0, Math.min($duration, (x / canvas.width) * $duration));
  }

  function draw() {
    if (!ctx) return;
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);

    // Background
    ctx.fillStyle = "#1a1a1a";
    ctx.fillRect(0, 0, w, h);

    // Clip span region
    if ($duration > 0) {
      const x0 = timeToX($cursor);
      const x1 = timeToX($cursor + $clipSpan);
      ctx.fillStyle = "rgba(0, 100, 200, 0.15)";
      ctx.fillRect(x0, 0, x1 - x0, h);
    }

    // Markers
    for (const m of $markers) {
      const x = timeToX(m.start_time);
      ctx.fillStyle = "#22aa44";
      ctx.fillRect(x - 1, 0, 3, h);
    }

    // Cursor
    if ($duration > 0) {
      const cx = timeToX($cursor);
      ctx.fillStyle = "#ff4444";
      ctx.fillRect(cx - 1, 0, 3, h);
    }

    // Play position
    if ($playPos !== null && $duration > 0) {
      const px = timeToX($playPos);
      ctx.fillStyle = "#ffaa00";
      ctx.fillRect(px - 1, 0, 2, h);
    }

    // Time labels
    if ($duration > 0) {
      ctx.fillStyle = "#888";
      ctx.font = "11px monospace";
      const step = Math.max(10, Math.pow(10, Math.floor(Math.log10($duration / 5))));
      for (let t = 0; t <= $duration; t += step) {
        const x = timeToX(t);
        ctx.fillText(formatTime(t), x + 2, h - 4);
        ctx.fillRect(x, h - 16, 1, 16);
      }
    }
  }

  function formatTime(s: number): string {
    const m = Math.floor(s / 60);
    const sec = (Math.floor(s % 60 * 10) / 10).toFixed(1);
    return `${m}:${sec.padStart(4, "0")}`;
  }

  function handleMouseDown(e: MouseEvent) {
    if ($locked) return;
    dragging = true;
    const time = xToTime(e.offsetX);
    $cursor = time;
    onCursorChange(time);
  }

  function handleMouseMove(e: MouseEvent) {
    if (!dragging || $locked) return;
    const time = xToTime(e.offsetX);
    $cursor = time;
    onCursorChange(time);
  }

  function handleMouseUp() {
    dragging = false;
  }

  function handleDblClick(e: MouseEvent) {
    const time = xToTime(e.offsetX);
    for (const m of $markers) {
      const mx = timeToX(m.start_time);
      if (Math.abs(e.offsetX - mx) < 8) {
        onMarkerClick(m);
        return;
      }
    }
    onSeek(time);
  }

  function handleContextMenu(e: MouseEvent) {
    e.preventDefault();
    for (const m of $markers) {
      const mx = timeToX(m.start_time);
      if (Math.abs(e.offsetX - mx) < 8) {
        onMarkerDelete(m.output_path);
        return;
      }
    }
  }

  // Redraw on any state change
  $effect(() => {
    void $duration; void $cursor; void $playPos; void $markers; void $clips; void $spread; void $clipSpan;
    draw();
  });

  onMount(() => {
    ctx = canvas.getContext("2d")!;
    const obs = new ResizeObserver(() => {
      canvas.width = canvas.clientWidth;
      canvas.height = HEIGHT;
      draw();
    });
    obs.observe(canvas);
    return () => obs.disconnect();
  });
</script>

<canvas
  bind:this={canvas}
  style="width:100%;height:{HEIGHT}px"
  onmousedown={handleMouseDown}
  onmousemove={handleMouseMove}
  onmouseup={handleMouseUp}
  onmouseleave={handleMouseUp}
  ondblclick={handleDblClick}
  oncontextmenu={handleContextMenu}
></canvas>

<style>
  canvas {
    display: block;
    background: #1a1a1a;
    cursor: crosshair;
  }
</style>
