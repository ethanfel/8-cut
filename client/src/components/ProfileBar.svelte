<script lang="ts">
  import { onMount } from "svelte";
  import { getProfiles } from "$lib/api";
  import { profile, subprofiles } from "$lib/stores";

  let profiles = $state<string[]>([]);

  onMount(async () => {
    profiles = await getProfiles();
    if (profiles.length && !profiles.includes($profile)) {
      $profile = profiles[0];
    }
  });

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
