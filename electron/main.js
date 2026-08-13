const { app, BrowserWindow, WebContentsView, session, dialog, ipcMain, shell, net } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const fsp = require("fs/promises");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const PORT = 5050;
const APP_URL = `http://${HOST}:${PORT}`;
const HOME_URL = "https://rebrickable.com";
const LOGIN_URL = "https://rebrickable.com/login/";
const PARTITION = "persist:rebrickable";
const TOOLBAR_HEIGHT = 52;
const TABLE_WAIT_MS = 90000;

const APP_AUTHOR = "红泥小火炉";
const APP_NAME = "Rebrickable 零件导出";

const INVENTORY_RE = /rebrickable\.com\/inventory\/(\d+)/i;
const SET_RE = /rebrickable\.com\/sets\/([^/?#]+)/i;
const MOC_RE = /rebrickable\.com\/mocs\/([^/?#]+)/i;

let mainWindow = null;
let toolbarView = null;
let contentView = null;
let backendProcess = null;
let lastState = {};
let exportInProgress = false;

function chromeUserAgent() {
  const chrome = process.versions.chrome || "131.0.0.0";
  if (process.platform === "win32") {
    return `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${chrome} Safari/537.36`;
  }
  if (process.platform === "linux") {
    return `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${chrome} Safari/537.36`;
  }
  return `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${chrome} Safari/537.36`;
}

function applySiteSessionFingerprint(ses) {
  const ua = chromeUserAgent();
  ses.setUserAgent(ua);
  const proxyReady = ses.setProxy({ mode: "system" }).catch((err) => {
    console.warn("Failed to apply system proxy:", err.message);
  });
  ses.webRequest.onErrorOccurred({ urls: ["*://*/*"] }, (details) => {
    if (
      details.error === "net::ERR_CONNECTION_CLOSED" ||
      details.error === "net::ERR_CONNECTION_RESET" ||
      details.error === "net::ERR_NAME_NOT_RESOLVED" ||
      details.error === "net::ERR_QUIC_PROTOCOL_ERROR"
    ) {
      console.warn(`[net] ${details.error} ${details.url}`);
    }
  });
  return proxyReady;
}

async function pageLooksLikeCloudflare(webContents) {
  try {
    return await webContents.executeJavaScript(`
      (() => {
        const text = (document.body && document.body.innerText) || "";
        const title = document.title || "";
        return /just a moment/i.test(title)
          || /just a moment/i.test(text)
          || text.includes("正在验证")
          || text.includes("请稍候")
          || text.includes("请验证您是真人")
          || text.includes("正在进行安全验证")
          || !!(document.querySelector(
            "#challenge-running, #challenge-stage, iframe[src*='challenges.cloudflare.com']"
          ));
      })()
    `);
  } catch {
    return false;
  }
}

// Must run before app is ready. A fake Chrome version (or leaking "Electron/")
// makes Cloudflare's JS challenge fail and reload forever.
app.commandLine.appendSwitch(
  "disable-features",
  "ThirdPartyCookiePhaseout,TrackingProtection3pcd"
);
// Cloudflare challenge uses HTTP/3 + WebRTC STUN (UDP). Behind an HTTP proxy
// those probes hang; force TCP and skip unproxied UDP so the check can finish.
app.commandLine.appendSwitch("disable-http3");
app.commandLine.appendSwitch("disable-quic");
app.userAgentFallback = chromeUserAgent();

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

function parsePage(url) {
  if (!url) return null;
  const inventory = url.match(INVENTORY_RE);
  if (inventory) {
    return { kind: "inventory", id: inventory[1] };
  }
  const setMatch = url.match(SET_RE);
  if (setMatch) {
    return { kind: "set", id: decodeURIComponent(setMatch[1]) };
  }
  const mocMatch = url.match(MOC_RE);
  if (mocMatch) {
    return { kind: "moc", id: decodeURIComponent(mocMatch[1]) };
  }
  return null;
}

function tableUrl(parsed) {
  if (parsed.kind === "inventory") {
    return `https://rebrickable.com/inventory/${parsed.id}/parts/?format=table`;
  }
  if (parsed.kind === "set") {
    return `https://rebrickable.com/sets/${parsed.id}/parts/?format=table`;
  }
  return `https://rebrickable.com/mocs/${parsed.id}/parts/?format=table`;
}

function fileStem(parsed) {
  if (parsed.kind === "inventory") return `inventory_${parsed.id}`;
  if (parsed.kind === "set") return `set_${parsed.id}`;
  return `moc_${parsed.id}`;
}

function normalizeUrl(input) {
  const trimmed = String(input || "").trim();
  if (!trimmed) return HOME_URL;
  if (/^\d+$/.test(trimmed)) {
    return `https://rebrickable.com/inventory/${trimmed}/parts/?format=table`;
  }
  if (/^https?:\/\//i.test(trimmed)) return trimmed;
  return `https://${trimmed}`;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function siteSession() {
  return session.fromPartition(PARTITION);
}

async function isLoggedIn() {
  const cookies = await siteSession().cookies.get({ url: HOME_URL });
  return cookies.some((cookie) => cookie.name === "sessionid");
}

function toPythonCookies(electronCookies) {
  return electronCookies.map((cookie) => {
    const payload = {
      name: cookie.name,
      value: cookie.value,
      domain: cookie.domain,
      path: cookie.path,
      secure: cookie.secure,
      httpOnly: cookie.httpOnly,
    };
    if (typeof cookie.expirationDate === "number") {
      payload.expiry = Math.floor(cookie.expirationDate);
    }
    if (cookie.sameSite === "no_restriction") {
      payload.sameSite = "None";
    } else if (cookie.sameSite === "lax" || cookie.sameSite === "strict") {
      payload.sameSite = cookie.sameSite === "lax" ? "Lax" : "Strict";
    }
    return payload;
  });
}

function layoutViews() {
  if (!mainWindow || !toolbarView || !contentView) return;
  const [width, height] = mainWindow.getContentSize();
  toolbarView.setBounds({ x: 0, y: 0, width, height: TOOLBAR_HEIGHT });
  contentView.setBounds({
    x: 0,
    y: TOOLBAR_HEIGHT,
    width,
    height: Math.max(0, height - TOOLBAR_HEIGHT),
  });
}

function canGoBack(wc) {
  if (wc.navigationHistory && typeof wc.navigationHistory.canGoBack === "function") {
    return wc.navigationHistory.canGoBack();
  }
  return wc.canGoBack();
}

function canGoForward(wc) {
  if (wc.navigationHistory && typeof wc.navigationHistory.canGoForward === "function") {
    return wc.navigationHistory.canGoForward();
  }
  return wc.canGoForward();
}

async function emitState(partial = {}) {
  if (!contentView || !toolbarView) return;
  const wc = contentView.webContents;
  const url = wc.getURL();
  const parsed = parsePage(url);
  const loggedIn = await isLoggedIn();
  const cloudflare = !partial.message && (await pageLooksLikeCloudflare(wc));
  if (typeof partial.exporting === "boolean") {
    exportInProgress = partial.exporting;
  }
  lastState = {
    url,
    canGoBack: canGoBack(wc),
    canGoForward: canGoForward(wc),
    loggedIn,
    exportable: Boolean(parsed) && !exportInProgress && !cloudflare,
    exporting: exportInProgress,
    message: partial.message || (cloudflare ? "请完成安全验证后继续" : ""),
    statusKind: partial.statusKind || (cloudflare ? "warn" : ""),
  };
  if (!toolbarView.webContents.isDestroyed()) {
    toolbarView.webContents.send("shell:state", lastState);
  }
}

function loadInContent(url) {
  if (!contentView) return;
  contentView.webContents.loadURL(url);
}

async function navigateAndWait(webContents, url) {
  const current = webContents.getURL();
  if (current === url) {
    return;
  }

  await new Promise((resolve, reject) => {
    let settled = false;
    const finish = (err) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      webContents.removeListener("did-finish-load", onFinish);
      webContents.removeListener("did-fail-load", onFail);
      if (err) reject(err);
      else resolve();
    };
    const onFinish = () => finish();
    const onFail = (_event, errorCode, errorDescription, _validatedURL, isMainFrame) => {
      if (!isMainFrame || errorCode === -3) return;
      finish(new Error(errorDescription || `页面加载失败 (${errorCode})`));
    };
    const timer = setTimeout(() => finish(new Error("页面加载超时")), 60000);
    webContents.once("did-finish-load", onFinish);
    webContents.on("did-fail-load", onFail);
    webContents.loadURL(url);
  });
}

async function pageHasPartsTable(webContents) {
  return webContents.executeJavaScript(
    `(() => {
      const text = (document.body && document.body.innerText) || "";
      if (/just a moment/i.test(text) || text.includes("正在验证") || text.includes("请稍候")) {
        return false;
      }
      return !!document.querySelector("table tr td");
    })()`
  );
}

async function waitForPartsTable(webContents, timeoutMs = TABLE_WAIT_MS) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const url = webContents.getURL().toLowerCase();
    if (url.includes("/login")) {
      throw new Error("需要登录后才能查看该清单，请先登录。");
    }
    if (await pageHasPartsTable(webContents)) {
      return;
    }
    await sleep(500);
  }
  throw new Error("未能找到零件表格。请打开套装或零件清单，若页面正在验证请稍后再试。");
}

async function exportCurrent() {
  if (!contentView || !mainWindow) {
    throw new Error("窗口尚未就绪");
  }

  const webContents = contentView.webContents;
  const parsed = parsePage(webContents.getURL());
  if (!parsed) {
    throw new Error("请先打开套装、MOC 或零件清单页面");
  }

  const { filePath, canceled } = await dialog.showSaveDialog(mainWindow, {
    title: "保存 Excel",
    defaultPath: `${fileStem(parsed)}.xlsx`,
    filters: [{ name: "Excel 文件", extensions: ["xlsx"] }],
  });
  if (canceled || !filePath) {
    await emitState({ exporting: false });
    return { ok: false, canceled: true };
  }

  await emitState({
    exporting: true,
    exportable: false,
    message: "正在打开零件表格…",
    statusKind: "warn",
  });

  try {
    const currentUrl = webContents.getURL();
    const target = tableUrl(parsed);
    const alreadyTable =
      /[?&]format=table(?:&|$)/i.test(currentUrl) &&
      (await pageHasPartsTable(webContents));
    if (!alreadyTable) {
      await navigateAndWait(webContents, target);
      await waitForPartsTable(webContents);
    }

    await emitState({
      exporting: true,
      exportable: false,
      message: "正在生成 Excel…",
      statusKind: "warn",
    });

    const html = await webContents.executeJavaScript(
      "document.documentElement.outerHTML"
    );
    const cookies = toPythonCookies(
      await siteSession().cookies.get({ url: HOME_URL })
    );

    const response = await net.fetch(`${APP_URL}/api/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        html,
        cookies,
        file_stem: fileStem(parsed),
      }),
    });

    const contentType = response.headers.get("content-type") || "";
    if (!response.ok || contentType.includes("application/json")) {
      let message = "导出失败";
      try {
        const data = await response.json();
        if (data && data.message) message = data.message;
      } catch {
        message = `导出失败 (${response.status})`;
      }
      throw new Error(message);
    }

    const buffer = Buffer.from(await response.arrayBuffer());
    await fsp.writeFile(filePath, buffer);

    await emitState({
      exporting: false,
      message: "导出成功",
      statusKind: "ok",
    });
    return { ok: true, filePath };
  } catch (error) {
    await emitState({
      exporting: false,
      message: error.message || "导出失败",
      statusKind: "err",
    });
    throw error;
  }
}

function createWindow() {
  const ua = chromeUserAgent();
  const ses = siteSession();
  const sessionReady = applySiteSessionFingerprint(ses);

  mainWindow = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 900,
    minHeight: 640,
    title: APP_NAME,
    autoHideMenuBar: true,
    backgroundColor: "#121820",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  toolbarView = new WebContentsView({
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  contentView = new WebContentsView({
    webPreferences: {
      partition: PARTITION,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  mainWindow.contentView.addChildView(contentView);
  mainWindow.contentView.addChildView(toolbarView);
  layoutViews();

  contentView.webContents.setUserAgent(ua);
  contentView.webContents.on("did-create-window", (childWindow) => {
    childWindow.webContents.setUserAgent(ua);
  });
  contentView.webContents.setWindowOpenHandler(({ url }) => {
    const allowed =
      url.startsWith("https://rebrickable.com") ||
      url.includes("accounts.google.com") ||
      url.includes("facebook.com") ||
      url.includes("appleid.apple.com");
    if (allowed) {
      return {
        action: "allow",
        overrideBrowserWindowOptions: {
          width: 520,
          height: 740,
          autoHideMenuBar: true,
          webPreferences: {
            partition: PARTITION,
          },
        },
      };
    }
    shell.openExternal(url);
    return { action: "deny" };
  });

  const syncNav = () => {
    if (exportInProgress) {
      emitState({
        exporting: true,
        message: lastState.message,
        statusKind: lastState.statusKind,
      });
      return;
    }
    emitState();
  };
  contentView.webContents.on("did-navigate", syncNav);
  contentView.webContents.on("did-navigate-in-page", syncNav);
  contentView.webContents.on("did-finish-load", syncNav);
  contentView.webContents.on("did-stop-loading", syncNav);

  toolbarView.webContents.loadFile(path.join(__dirname, "toolbar.html"));
  toolbarView.webContents.on("did-finish-load", () => {
    emitState();
  });
  Promise.resolve(sessionReady).finally(() => {
    if (contentView && !contentView.webContents.isDestroyed()) {
      contentView.webContents.loadURL(HOME_URL);
    }
  });

  mainWindow.on("resize", layoutViews);
  mainWindow.on("ready-to-show", layoutViews);
  mainWindow.on("closed", () => {
    mainWindow = null;
    toolbarView = null;
    contentView = null;
  });
}

ipcMain.on("nav:back", () => {
  if (!contentView) return;
  const wc = contentView.webContents;
  if (canGoBack(wc)) wc.goBack();
});

ipcMain.on("nav:forward", () => {
  if (!contentView) return;
  const wc = contentView.webContents;
  if (canGoForward(wc)) wc.goForward();
});

ipcMain.on("nav:reload", () => {
  if (contentView) contentView.webContents.reload();
});

ipcMain.on("nav:go", (_event, url) => {
  loadInContent(normalizeUrl(url));
});

ipcMain.on("nav:login", () => {
  loadInContent(LOGIN_URL);
});

ipcMain.handle("session:logout", async () => {
  await siteSession().clearStorageData({
    storages: ["cookies"],
  });
  loadInContent(HOME_URL);
  await emitState({ message: "已退出登录", statusKind: "warn" });
  return { ok: true };
});

ipcMain.handle("export:current", async () => {
  return exportCurrent();
});

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
