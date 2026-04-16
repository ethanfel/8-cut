<script lang="ts">
  import { onMount } from "svelte";
  import { getProfiles, setServer, getServer } from "$lib/api";
  import { profile, subprofiles, serverUrl } from "$lib/stores";
  import { saveSettings } from "$lib/settings";

  let profiles = $state<string[]>([]);
  let serverInput = $state(getServer());

  onMount(async () => {
    serverInput = getServer();
    try {
      profiles = await getProfiles();
      if (profiles.length && !profiles.includes($profile)) {
        $profile = profiles[0];
      }
    } catch { /* server not reachable yet */ }
  });

  function applyServer() {
    const url = serverInput.replace(/\/+$/, "");
    setServer(url);
    $serverUrl = url;
    saveSettings();
    // Reload profiles from new server
    getProfiles().then(p => { profiles = p; }).catch(() => {});
  }

  function addSubprofile() {
    const name = prompt("Subprofile suffix:");
    if (name && !$subprofiles.includes(name)) {
      $subprofiles = [...$subprofiles, name];
    }
  }

  function removeSubprofile(name: string) {
    $subprofiles = $subprofiles.filter(s => s !== name);
  }
</script>

<div class="profile-bar">
  <input
    class="server-input"
    type="text"
    bind:value={serverInput}
    onkeydown={(e) => { if (e.key === "Enter") applyServer(); }}
    placeholder="http://host:8000"
  />
  <button onclick={applyServer}>Set</button>

  <select bind:value={$profile}>
    {#each profiles as p}
      <option value={p}>{p}</option>
    {/each}
  </select>

  <span class="subs">
    {#each $subprofiles as sub}
      <span class="sub-tag" oncontextmenu={(e) => { e.preventDefault(); removeSubprofile(sub); }}>
        {sub}
      </span>
    {/each}
    <button onclick={addSubprofile}>+</button>
  </span>
</div>

<style>
  .profile-bar {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px;
    font-size: 12px;
  }
  .server-input {
    width: 180px;
    background: #2d2d2d;
    color: #e0e0e0;
    border: 1px solid #444;
    padding: 2px 4px;
    font-size: 11px;
  }
  select { background: #2d2d2d; color: #e0e0e0; border: 1px solid #444; }
  .subs { display: flex; gap: 4px; align-items: center; }
  .sub-tag {
    background: #444;
    padding: 2px 6px;
    border-radius: 3px;
    cursor: context-menu;
    font-size: 11px;
  }
  button { background: #333; color: #e0e0e0; border: 1px solid #555; padding: 1px 6px; cursor: pointer; }
</style>
