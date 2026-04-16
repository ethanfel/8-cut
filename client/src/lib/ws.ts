import { getServer } from "./api";
import { exportStatus, exportCompleted } from "./stores";

let socket: WebSocket | null = null;

export function connectExportWs() {
  const wsUrl = getServer().replace(/^http/, "ws") + "/ws/export";
  socket = new WebSocket(wsUrl);

  socket.onmessage = (event) => {
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
  };

  socket.onclose = () => {
    // Reconnect after 2s
    setTimeout(connectExportWs, 2000);
  };
}

export function disconnectExportWs() {
  if (socket) {
    socket.onclose = null; // prevent reconnect
    socket.close();
    socket = null;
  }
}
