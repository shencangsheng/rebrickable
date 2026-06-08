const { app, BrowserWindow, dialog } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const PORT = 5050;
const APP_URL = `http://${HOST}:${PORT}`;

let mainWindow = null;
let backendProcess = null;

function projectRoot() {
  return path.join(__dirname, "..");
}

function resolveBackendCommand() {
  const root = projectRoot();

  if (app.isPackaged) {
    const resources = process.resourcesPath;
    if (process.platform === "win32") {
      const bundled = path.join(resources, "backend", "rebrickable-backend.exe");
      if (fs.existsSync(bundled)) {
        return { command: bundled, args: [], cwd: path.dirname(bundled) };
      }
    } else {
      const bundled = path.join(resources, "backend", "rebrickable-backend");
      if (fs.existsSync(bundled)) {
        return { command: bundled, args: [], cwd: path.dirname(bundled) };
      }
    }
  }

  if (process.platform === "win32") {
    const bundled = path.join(root, "dist", "rebrickable-backend.exe");
    if (fs.existsSync(bundled)) {
      return { command: bundled, args: [], cwd: path.dirname(bundled) };
    }
    const venvPython = path.join(root, ".venv", "Scripts", "python.exe");
    if (fs.existsSync(venvPython)) {
      return {
        command: venvPython,
        args: [path.join(root, "desktop.py")],
        cwd: root,
      };
    }
    return {
      command: "python",
      args: [path.join(root, "desktop.py")],
      cwd: root,
    };
  }

  const bundledMac = path.join(
    root,
    "dist",
    "rebrickable-backend",
    "rebrickable-backend"
  );
  if (fs.existsSync(bundledMac)) {
    return { command: bundledMac, args: [], cwd: path.dirname(bundledMac) };
  }

  const venvPython = path.join(root, ".venv", "bin", "python");
  if (fs.existsSync(venvPython)) {
    return {
      command: venvPython,
      args: [path.join(root, "app.py")],
      cwd: root,
    };
  }

  return {
    command: "python3",
    args: [path.join(root, "app.py")],
    cwd: root,
  };
}

function waitForServer(timeoutMs = 60000) {
  const started = Date.now();

  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get(`${APP_URL}/api/status`, (res) => {
        res.resume();
        if (res.statusCode === 200) {
          resolve();
          return;
        }
        retry();
      });
      req.on("error", retry);
      req.setTimeout(2000, () => {
        req.destroy();
        retry();
      });
    };

    const retry = () => {
      if (Date.now() - started > timeoutMs) {
        reject(new Error("Flask backend did not start in time"));
        return;
      }
      setTimeout(tick, 500);
    };

    tick();
  });
}

function startBackend() {
  const { command, args, cwd } = resolveBackendCommand();
  backendProcess = spawn(command, args, {
    cwd,
    stdio: "inherit",
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
  });

  backendProcess.on("error", (err) => {
    dialog.showErrorBox(
      "Backend failed to start",
      `${err.message}\n\nMake sure Python is installed, or build the backend with PyInstaller first.`
    );
    app.quit();
  });

  backendProcess.on("exit", (code) => {
    if (code !== null && code !== 0 && mainWindow && !mainWindow.isDestroyed()) {
      dialog.showErrorBox("Backend exited", `Process exited with code ${code}`);
      app.quit();
    }
  });
}

function stopBackend() {
  if (!backendProcess) {
    return;
  }
  if (process.platform === "win32") {
    spawn("taskkill", ["/pid", String(backendProcess.pid), "/f", "/t"]);
  } else {
    backendProcess.kill("SIGTERM");
  }
  backendProcess = null;
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 560,
    height: 720,
    minWidth: 480,
    minHeight: 600,
    title: APP_NAME,
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadURL(APP_URL);

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

const APP_AUTHOR = "红泥小火炉";
const APP_NAME = "Rebrickable 零件导出";

app.setAboutPanelOptions({
  applicationName: APP_NAME,
  applicationVersion: app.getVersion(),
  copyright: `Copyright © ${APP_AUTHOR}`,
  credits: `开发者：${APP_AUTHOR}`,
});

app.whenReady().then(async () => {
  startBackend();
  try {
    await waitForServer();
    createWindow();
  } catch (err) {
    dialog.showErrorBox("Startup failed", err.message);
    stopBackend();
    app.quit();
  }
});

app.on("window-all-closed", () => {
  stopBackend();
  app.quit();
});

app.on("before-quit", () => {
  stopBackend();
});
