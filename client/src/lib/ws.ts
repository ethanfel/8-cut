import { getServer } from "./api";
import { exportStatus, exportCompleted } from "./stores";

let socket: WebSocket | null = null;
let reconnectDelay = 2000;

export function connectExportWs() {
  const wsUrl = getServer().replace(/^http/, "ws") + "/ws/export";
  socket = new WebSocket(wsUrl);

  socket.onopen = () => {
    reconnectDelay = 2000; // reset backoff on successful connect
  };

  socket.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      switch (msg.type) {
        case "clip_done":
          exportCompleted.update(n => n + 1);
          break;
        case "all_done":
          exportStatus.set("done");
          break;
        case "error":
          exportStatus.set("error");
          console.error("Export error:", msg.msg);
          break;
      }
    } catch (e) {
      console.error("Failed to parse WebSocket message:", e);
    }
  };

  socket.onclose = () => {
    // Reconnect with exponential backoff, max 30s
    setTimeout(connectExportWs, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 30000);
  };
}

export function disconnectExportWs() {
  if (socket) {
    socket.onclose = null; // prevent reconnect
    socket.close();
    socket = null;
  }
}
