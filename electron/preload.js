const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("shell", {
  back: () => ipcRenderer.send("nav:back"),
  forward: () => ipcRenderer.send("nav:forward"),
  reload: () => ipcRenderer.send("nav:reload"),
  go: (url) => ipcRenderer.send("nav:go", url),
  login: () => ipcRenderer.send("nav:login"),
  logout: () => ipcRenderer.invoke("session:logout"),
  exportCurrent: () => ipcRenderer.invoke("export:current"),
  onState: (callback) => {
    ipcRenderer.on("shell:state", (_event, state) => {
      callback(state);
    });
  },
});
